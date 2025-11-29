# -*- coding: utf-8 -*-
import os
import time
import json
import hashlib
import shutil
import threading
import traceback
import concurrent.futures
import random
from typing import List, Dict, Any

from flask import Flask, send_file, send_from_directory, request, jsonify, Response, stream_with_context, redirect
from elasticsearch import Elasticsearch, helpers, exceptions as es_exceptions
from PIL import Image

# =========================
# 1. 配置与常量
# =========================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
IMAGE_FOLDER = os.path.join(BASE_DIR, 'meme_images')
THUMBNAIL_FOLDER = os.path.join(BASE_DIR, 'meme_images_thumbnail')
TRASH_SUBDIR = 'trash_bin'
TRASH_TAG = TRASH_SUBDIR
TRASH_DIR = os.path.join(IMAGE_FOLDER, TRASH_SUBDIR)
DB_DIR = os.path.join(BASE_DIR, 'db') 
THUMBNAIL_MAX_SIZE = 600

os.makedirs(IMAGE_FOLDER, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

LEGACY_TRASH_DIR = os.path.join(BASE_DIR, 'trash_bin')
if os.path.exists(LEGACY_TRASH_DIR) and os.path.abspath(LEGACY_TRASH_DIR) != os.path.abspath(TRASH_DIR):
    for entry in os.listdir(LEGACY_TRASH_DIR):
        src_entry = os.path.join(LEGACY_TRASH_DIR, entry)
        dst_entry = os.path.join(TRASH_DIR, entry)
        if os.path.exists(dst_entry):
            if os.path.isdir(dst_entry):
                shutil.rmtree(dst_entry)
            else:
                os.remove(dst_entry)
        shutil.move(src_entry, dst_entry)
    try:
        os.rmdir(LEGACY_TRASH_DIR)
    except OSError:
        pass

# ES 配置
ELASTICSEARCH_HOSTS = ['http://localhost:9200']

IMAGE_INDEX = 'meme_images_v2'      
META_INDEX = 'meme_system_config'   
COMMON_TAGS_ID = 'common_tags_store'

ALLOWED_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}

def calculate_md5(file_path=None, file_stream=None):
    hash_md5 = hashlib.md5()
    if file_path:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
    elif file_stream:
        pos = file_stream.tell()
        if pos != 0: file_stream.seek(0)
        for chunk in iter(lambda: file_stream.read(4096), b""):
            hash_md5.update(chunk)
        file_stream.seek(pos)
    return hash_md5.hexdigest()

# =========================
# 2. DataManager (Core)
# =========================

class DataManager:
    def __init__(self):
        print("[DataManager] 初始化 (MD5 智能修复模式)...")
        self.es = Elasticsearch(hosts=ELASTICSEARCH_HOSTS)
        
        # 维护模式标志：# 定义一把全局锁，用于互斥“扫描”和“导入”，暂停后台删除逻辑，防止导入数据被误删
        self.task_lock = threading.Lock()
        
        try:
            if not self.es.ping():
                print("Warning: 无法连接到 Elasticsearch。请确保 ES 服务已启动。")
        except Exception as e:
            print(f"Error connecting to ES: {e}")
            
        self.check_and_create_index()
        
        self.synonym_map = {}
        self.synonym_leaf_to_root = {}
        self._load_metadata()

        # 缩略图预检查：启动后台线程生成缺失的缩略图
        threading.Thread(target=self._generate_missing_thumbnails, daemon=True).start()

        # 启动后台同步：负责将文件重命名为 MD5 并同步到 ES
        threading.Thread(target=self._sync_files_to_es, daemon=True).start()

    def check_and_create_index(self):
        """创建索引：ID=MD5, filename仅作物理路径记录"""
        if not self.es.indices.exists(index=IMAGE_INDEX):
            print(f"[ES] 创建新索引: {IMAGE_INDEX}")
            mapping = {
                "mappings": {
                    "properties": {
                        "filename": {"type": "keyword"}, 
                        "md5": {"type": "keyword"},
                        "tags": {
                            "type": "text",
                            "analyzer": "ik_max_word",      
                            "search_analyzer": "ik_smart",  
                            "fields": {
                                "raw": {
                                    "type": "keyword",      
                                    "ignore_above": 256
                                }
                            }
                        }
                    }
                }
            }
            try:
                self.es.indices.create(index=IMAGE_INDEX, body=mapping)
            except es_exceptions.RequestError as e:
                if 'resource_already_exists_exception' not in str(e): raise e

        if not self.es.indices.exists(index=META_INDEX):
            self.es.indices.create(index=META_INDEX, body={
                "mappings": {
                    "properties": {
                        "common_tags": {"type": "object", "enabled": False}, 
                        "tag_synonyms": {"type": "object", "enabled": False}
                    }
                }
            })

    def _load_metadata(self):
        try:
            doc = self.es.get(index=META_INDEX, id=COMMON_TAGS_ID)
            src = doc['_source']
            self.synonym_map = src.get('tag_synonyms', {})
            self._rebuild_reverse_map()
        except Exception:
            print("[Meta] 暂无元数据，初始化为空")
            self.synonym_map = {}
            self._save_metadata({}) 

    def _save_metadata(self, common_tags_dict=None):
        body = {"tag_synonyms": self.synonym_map}
        if common_tags_dict is not None:
            body["common_tags"] = common_tags_dict
        else:
            try:
                old = self.es.get(index=META_INDEX, id=COMMON_TAGS_ID)
                body["common_tags"] = old['_source'].get("common_tags", {})
            except:
                body["common_tags"] = {}
        self.es.index(index=META_INDEX, id=COMMON_TAGS_ID, body=body)

    def _rebuild_reverse_map(self):
        self.synonym_leaf_to_root = {}
        for main, children in self.synonym_map.items():
            self.synonym_leaf_to_root[main] = main
            for child in children:
                self.synonym_leaf_to_root[child] = main

    # --- 缩略图工具 ---
    def _path_join(self, base_dir, relative_path):
        return os.path.join(base_dir, *relative_path.split('/'))

    def _thumbnail_rel_path(self, filename):
        base, _ = os.path.splitext(filename)
        return f"{base}_thumbnail.jpg"

    def _extract_random_frame(self, img: Image.Image):
        frames = getattr(img, "n_frames", 1)
        if frames and frames > 1:
            try:
                img.seek(random.randrange(frames))
            except Exception:
                img.seek(0)
        return img.copy()

    def _create_thumbnail_file(self, source_path, thumb_path):
        with Image.open(source_path) as img:
            frame = self._extract_random_frame(img)
            if frame.mode not in ("RGB", "L"):
                frame = frame.convert("RGB")
            elif frame.mode == "L":
                frame = frame.convert("RGB")

            frame.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.LANCZOS)
            os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
            frame.save(thumb_path, "JPEG", quality=85, optimize=True)

    def _ensure_thumbnail_exists(self, filename):
        if not filename:
            return None
        thumb_rel = self._thumbnail_rel_path(filename)
        thumb_full = self._path_join(THUMBNAIL_FOLDER, thumb_rel)
        src_full = self._path_join(IMAGE_FOLDER, filename)
        try:
            if not os.path.exists(src_full):
                return None
            if os.path.exists(thumb_full):
                return thumb_rel
            self._create_thumbnail_file(src_full, thumb_full)
            return thumb_rel
        except Exception as e:
            print(f"[Thumb] 生成缩略图失败 {thumb_rel}: {e}")
            traceback.print_exc()
            return None

    def _is_trash_path(self, rel_path):
        if not rel_path:
            return False
        rel_path = rel_path.replace('\\', '/')
        return rel_path.startswith(f"{TRASH_SUBDIR}/")

    def _ensure_trash_tag(self, tags, should_be_trash):
        base_tags = set(tags or [])
        if should_be_trash:
            base_tags.add(TRASH_TAG)
        else:
            base_tags.discard(TRASH_TAG)
        return sorted(base_tags)

    def _move_thumbnail_file(self, src_rel, dst_rel):
        src_thumb = self._path_join(THUMBNAIL_FOLDER, self._thumbnail_rel_path(src_rel))
        dst_thumb = self._path_join(THUMBNAIL_FOLDER, self._thumbnail_rel_path(dst_rel))
        if not os.path.exists(src_thumb):
            return
        os.makedirs(os.path.dirname(dst_thumb), exist_ok=True)
        if os.path.abspath(src_thumb) == os.path.abspath(dst_thumb):
            return
        if os.path.exists(dst_thumb):
            try:
                os.remove(dst_thumb)
            except Exception:
                pass
        try:
            shutil.move(src_thumb, dst_thumb)
        except Exception as e:
            print(f"[Thumb Move] 无法移动缩略图: {e}")

    def _build_image_payload(self, filename, tags=None, md5=None, score=None):
        thumb_rel = self._ensure_thumbnail_exists(filename)
        thumb_url = f"/thumbnails/{thumb_rel}" if thumb_rel else None
        data = {
            "filename": filename,
            "tags": tags or [],
            "url": f"/images/{filename}",
            "thumbnail_url": thumb_url or f"/images/{filename}",
            "md5": md5 if md5 else os.path.splitext(os.path.basename(filename))[0]
        }
        data["is_trashed"] = TRASH_TAG in (data["tags"] or [])
        if score is not None:
            data["score"] = score
        return data

    def _generate_missing_thumbnails(self):
        print("[Thumb] 开始检查/补全缩略图...")
        for root, _, files in os.walk(IMAGE_FOLDER):
            for fname in files:
                ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
                if ext not in ALLOWED_EXTS:
                    continue
                rel = os.path.relpath(os.path.join(root, fname), IMAGE_FOLDER).replace('\\', '/')
                self._ensure_thumbnail_exists(rel)
        print("[Thumb] 缩略图检查完成")

    # --- 多线程同步与修复逻辑 ---

    def _process_file_standardization(self, file_path):
        """
        核心修复逻辑：
        1. 计算 MD5。
        2. 在当前子目录内重命名为 MD5.ext。
        3. 返回 (MD5, 相对路径字符串)。
        """
        try:
            # 获取目录绝对路径和文件名
            dir_path = os.path.dirname(file_path)
            fname = os.path.basename(file_path)
            
            if '.' not in fname: return None
            ext = fname.rsplit('.', 1)[1].lower()
            if ext not in ALLOWED_EXTS: return None

            # 计算 MD5
            md5_val = calculate_md5(file_path=file_path)
            
            # 构造期望的标准文件名 (仅文件名部分)
            expected_name = f"{md5_val}.{ext}"
            
            # 构造最终的相对路径 (用于存入数据库)
            # 注意：如果发生了重命名，最后的文件名应该是 expected_name
            final_name_in_dir = expected_name
            
            # 如果当前文件名不是标准 MD5 格式，执行重命名（修复）
            if fname != expected_name:
                new_full_path = os.path.join(dir_path, expected_name)
                try:
                    if not os.path.exists(new_full_path):
                        os.rename(file_path, new_full_path)
                    else:
                        # 目标已存在，说明是同一目录下的重复文件，删除当前冗余副本
                        os.remove(file_path)
                except OSError:
                    return None 
            
            # 计算相对路径，并统一转为 / 分隔符 (Web 标准)
            # 此时文件物理路径已经是 dir_path/expected_name
            abs_final_path = os.path.join(dir_path, final_name_in_dir)
            rel_path = os.path.relpath(abs_final_path, IMAGE_FOLDER)
            rel_path = rel_path.replace('\\', '/') # 兼容 Windows
            
            return (md5_val, rel_path)
            
        except Exception:
            traceback.print_exc()
            return None


    def _sync_files_to_es(self):
        """
        后台线程：
        支持递归扫描子文件夹，路径存储为 "subdir/filename.ext"
        修改：发现重复MD5时，更新ES路径并删除旧文件
        """
        with self.task_lock:
            print("[Sync] 已获得锁，开始执行单次同步...")
            
            try:
                # 1. 获取 ES 现状 (白名单)
                es_doc_map = {}
                es_ids = set()
                try:
                    if self.es.indices.exists(index=IMAGE_INDEX):
                        scan_gen = helpers.scan(
                            self.es,
                            index=IMAGE_INDEX,
                            query={"query": {"match_all": {}}},
                            _source=["filename","tags"],
                            scroll='2m'
                        )
                        for hit in scan_gen:
                            mid = hit['_id']
                            es_ids.add(mid)
                            src = hit.get('_source') or {}
                            es_doc_map[mid] = src
                except Exception as e:
                    print(f"[Sync] ES 读取失败: {e}")

                # 2. 递归扫描磁盘 (os.walk)
                disk_md5_map = {} 
                files_to_process = [] 

                if os.path.exists(IMAGE_FOLDER):
                    for root, dirs, files in os.walk(IMAGE_FOLDER):
                        for fname in files:
                            full_path = os.path.join(root, fname)
                            rel_path = os.path.relpath(full_path, IMAGE_FOLDER).replace('\\', '/')
                            
                            fname_base = os.path.basename(fname)
                            fname_stem = fname_base.rsplit('.', 1)[0] if '.' in fname_base else fname_base
                            
                            # 判断文件名主体是否已经是已知的 MD5 ID
                            if fname_stem in es_ids:
                                # 即使命中白名单，也要记录当前实际路径，以便后续对比是否发生移动
                                disk_md5_map[fname_stem] = rel_path
                            else:
                                files_to_process.append(full_path)
                
                # 3. 多线程处理未知文件
                if files_to_process:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                        future_to_file = {executor.submit(self._process_file_standardization, fpath): fpath for fpath in files_to_process}
                        
                        for future in concurrent.futures.as_completed(future_to_file):
                            result = future.result()
                            if result:
                                md5_val, final_rel_name = result
                                disk_md5_map[md5_val] = final_rel_name
                
                # 4. 核心逻辑：计算增删改
                disk_ids = set(disk_md5_map.keys())
                
                to_add = disk_ids - es_ids
                to_delete = es_ids - disk_ids 

                # --- 新增逻辑：处理已存在但路径变更的文件 (去重与移动) ---
                update_actions = []
                for mid in disk_ids & es_ids:
                    doc = es_doc_map.get(mid, {})
                    old_path = doc.get('filename') or ''
                    old_tags = doc.get('tags') or []
                    new_path = disk_md5_map[mid]
                    path_changed = old_path != new_path
                    should_be_trash = self._is_trash_path(new_path)
                    has_trash = TRASH_TAG in old_tags
                    tags_need_update = should_be_trash != has_trash
                    doc_updates = {}
                    if path_changed:
                        doc_updates["filename"] = new_path
                    if tags_need_update:
                        doc_updates["tags"] = self._ensure_trash_tag(old_tags, should_be_trash)

                    if doc_updates:
                        update_actions.append({
                            "_op_type": "update",
                            "_index": IMAGE_INDEX,
                            "_id": mid,
                            "doc": doc_updates
                        })

                    if path_changed and old_path:
                        old_full_path = os.path.join(IMAGE_FOLDER, old_path)
                        new_full_path = os.path.join(IMAGE_FOLDER, new_path)
                        if os.path.abspath(old_full_path) != os.path.abspath(new_full_path):
                            if os.path.exists(old_full_path):
                                try:
                                    os.remove(old_full_path)
                                    print(f'[Sync] 去重: 已删除旧位置文件 {old_path}，保留新位置 {new_path}')
                                except Exception as e:
                                    print(f'[Sync] 删除旧文件失敗: {e}')
                if update_actions:
                    print(f"[Sync] 更新文件路径: {len(update_actions)} 条")
                    helpers.bulk(self.es, update_actions)
                # -------------------------------------------------------
                
                # 5. 执行常规增删
                if to_delete:
                    print(f"[Sync] 清理失效索引: {len(to_delete)} 条 (磁盘彻底缺失)")
                    actions = [{"_op_type": "delete", "_index": IMAGE_INDEX, "_id": mid} for mid in to_delete]
                    helpers.bulk(self.es, actions)
                
                if to_add:
                    print(f"[Sync] 新文件入库: {len(to_add)} 张")
                    actions = []
                    for mid in to_add:
                        rel_fname = disk_md5_map[mid]
                        actions.append({
                            "_index": IMAGE_INDEX,
                            "_id": mid,
                            "_source": {
                                "filename": rel_fname,
                                "md5": mid,
                                "tags": self._ensure_trash_tag([], self._is_trash_path(rel_fname))
                            }
                        })
                    if actions:
                        helpers.bulk(self.es, actions)
                    
            except Exception as e:
                print(f"[Sync] 异常: {e}")
                traceback.print_exc()
                    
            except Exception as e:
                print(f"[Sync] 异常: {e}")
                traceback.print_exc()
                
                # 3. 多线程处理未知文件 (计算MD5 + 重命名)
                if files_to_process:
                    # print(f"[Sync] 正在分析 {len(files_to_process)} 个新/变动文件...")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                        future_to_file = {executor.submit(self._process_file_standardization, entry): entry for entry in files_to_process}
                        
                        for future in concurrent.futures.as_completed(future_to_file):
                            result = future.result()
                            if result:
                                md5_val, final_name = result
                                # 记录修复后的 MD5，这会挽救那些“已导入数据但文件名不对”的记录
                                disk_md5_map[md5_val] = final_name
                
                # 4. 最终对比
                disk_ids = set(disk_md5_map.keys())
                
                to_add = disk_ids - es_ids
                to_delete = es_ids - disk_ids # 只有磁盘上怎么都找不到 MD5 的，才会被删
                
                # 5. 执行变更
                if to_delete:
                    print(f"[Sync] 清理失效索引: {len(to_delete)} 条 (磁盘彻底缺失)")
                    actions = [{"_op_type": "delete", "_index": IMAGE_INDEX, "_id": mid} for mid in to_delete]
                    helpers.bulk(self.es, actions)
                
                if to_add:
                    print(f"[Sync] 新文件入库: {len(to_add)} 张")
                    actions = []
                    for mid in to_add:
                        fname = disk_md5_map[mid]
                        self._ensure_thumbnail_exists(fname)
                        actions.append({
                            "_index": IMAGE_INDEX,
                            "_id": mid,
                            "_source": {
                                "filename": fname,
                                "md5": mid,
                                "tags": []
                            }
                        })
                    if actions:
                        helpers.bulk(self.es, actions)
                    
            except Exception as e:
                print(f"[Sync] 异常: {e}")
                traceback.print_exc()
        

    # --- 辅助功能 ---
    def get_all_variants(self, tag):
        root = self.synonym_leaf_to_root.get(tag, tag)
        variants = {root}
        if root in self.synonym_map:
            variants.update(self.synonym_map[root])
        return list(variants)

    def _includes_trash_tag(self, tags):
        if not tags:
            return False
        for tag in tags:
            if isinstance(tag, str) and tag.strip().lower() == TRASH_TAG:
                return True
        return False

    def _apply_trash_filter(self, bool_clause, allow_trash=False):
        if allow_trash:
            return
        must_not = bool_clause.setdefault("must_not", [])
        clause = {"term": {"tags.raw": TRASH_TAG}}
        if clause not in must_not:
            must_not.append(clause)

    # --- 业务逻辑 ---

    def get_next_untagged_image(self, current_filename=None, filter_type='untagged'):
        query = {"bool": {"must": [], "must_not": []}}
        
        if filter_type == 'untagged':
            query["bool"]["must_not"].append({"exists": {"field": "tags"}})
        elif filter_type == 'tagged':
            query["bool"]["must"].append({"exists": {"field": "tags"}})
        
        sort_order = [{"filename": "asc"}]
        
        if current_filename:
             query["bool"]["must"].append({"range": {"filename": {"gt": current_filename}}})
        
        self._apply_trash_filter(query["bool"])

        res = self.es.search(index=IMAGE_INDEX, body={"query": query, "sort": sort_order, "size": 1})
        
        hits = res['hits']['hits']
        if hits:
            src = hits[0]['_source']
            payload = self._build_image_payload(src['filename'], src.get("tags", []), src.get("md5", ""))
            payload["success"] = True
            return payload
        
        # 循环回到开头
        if current_filename:
            if filter_type == 'untagged':
                q2 = {"bool": {"must_not": [{"exists": {"field": "tags"}}], "must": []}}
            elif filter_type == 'tagged':
                q2 = {"bool": {"must": [{"exists": {"field": "tags"}}], "must_not": []}}
            else:
                q2 = {"bool": {"must": [{"match_all": {}}], "must_not": []}}
                
            self._apply_trash_filter(q2["bool"])
            res = self.es.search(index=IMAGE_INDEX, body={"query": q2, "sort": sort_order, "size": 1})
            if res['hits']['hits']:
                src = res['hits']['hits'][0]['_source']
                payload = self._build_image_payload(src['filename'], src.get("tags", []), src.get("md5", ""))
                payload["success"] = True
                payload["message"] = "循环回到第一张"
                return payload

        return {"success": False, "message": "没有更多图片了"}


    def save_tags(self, filename, tags):
        # filename 可能是 "subdir/md5.jpg"
        # 我们需要提取 "md5" 作为 doc_id
        basename = os.path.basename(filename)
        doc_id = basename.rsplit('.', 1)[0]
        
        cleaned_tags = sorted(list(set([t.strip() for t in tags if t.strip()])))
        try:
            self.es.update(index=IMAGE_INDEX, id=doc_id, body={"doc": {"tags": cleaned_tags}})
            return {"success": True}
        except Exception as e:
            print(f"Save tags error: {e}")
            return {"success": False}

    def get_common_tags(self, limit=100, offset=0, query=""):
        body = {
            "size": 0,
            "aggs": {
                "raw_tags": {
                    "terms": {"field": "tags.raw", "size": 5000}
                }
            }
        }
        try:
            res = self.es.search(index=IMAGE_INDEX, body=body)
            buckets = res['aggregations']['raw_tags']['buckets']
        except Exception:
            return {"tags": [], "total": 0}

        merged_stats = {} 
        for b in buckets:
            raw_tag = b['key']
            count = b['doc_count']
            root = self.synonym_leaf_to_root.get(raw_tag, raw_tag)
            if root not in merged_stats:
                merged_stats[root] = {"tag": root, "count": 0, "synonyms": self.synonym_map.get(root, [])}
            merged_stats[root]["count"] += count

        result_list = []
        q_lower = query.lower().strip()
        for root_tag, data in merged_stats.items():
            if q_lower:
                match_main = q_lower in root_tag.lower()
                match_syn = any(q_lower in s.lower() for s in data['synonyms'])
                if not (match_main or match_syn): continue
            result_list.append(data)

        result_list.sort(key=lambda x: x['count'], reverse=True)
        total = len(result_list)
        sliced = result_list[offset : offset + limit]
        return {"tags": sliced, "total": total}

    def search(self, include, exclude, offset, limit):
        must_clauses = []
        must_not_clauses = []

        for tag in include:
            variants = self.get_all_variants(tag)
            must_clauses.append({"terms": {"tags.raw": variants}})
        
        for tag in exclude:
            variants = self.get_all_variants(tag)
            must_not_clauses.append({"terms": {"tags.raw": variants}})

        allow_trash = self._includes_trash_tag(include)

        body = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "must_not": must_not_clauses
                }
            },
            "from": offset,
            "size": limit,
            "sort": [{"_score": "desc"}, {"filename": "asc"}]
        }
        self._apply_trash_filter(body["query"]["bool"], allow_trash=allow_trash)
        return self._format_es_response(self.es.search(index=IMAGE_INDEX, body=body))


    def semantic_search(self, query_text, offset, limit):
        if not query_text: return {"results": [], "total": 0}
        
        # 1. 分词处理 (保持原逻辑) [cite: 1, 2, 9]
        tokens = []
        try:
            analysis = self.es.indices.analyze(
                index=IMAGE_INDEX,
                body={"analyzer": "ik_smart", "text": query_text}
            )
            tokens = [t['token'] for t in analysis.get('tokens', [])]
        except Exception as e:
            # 异常处理 [cite: 3, 10]
            print(f"[Search] Analyze failed, falling back to split: {e}")
            tokens = query_text.strip().split()

        should_clauses = []
        
        # 2. 遍历分词构建“包含”查询
        for token in tokens:
            if len(token) < 1: continue
            
            # 获取同义词 [cite: 5, 11]
            variants = self.get_all_variants(token)
            
            if variants:
                # --- 修改开始 ---
                # 原逻辑使用 "terms" (精确匹配)，现改为 "wildcard" (包含匹配)
                # 因为 wildcard 只能针对单值，所以需要对 variants 里的每个词分别构建 wildcard

                # search_text = " ".join(variants)
        
                # # 使用 match (全文匹配) 替代 wildcard
                # should_clauses.append({
                #     # 对 tags 字段进行全文匹配，ES 会使用 ik_smart 分词器对 search_text 进行分词后再查询
                #     "match": { 
                #         "tags": { 
                #             "query": search_text,
                #             "operator": "or", # 只要匹配同义词中的任一个
                #             "boost": 2.0      # 提高全文匹配的权重
                #         }
                #     }
                # })


                variant_wildcards = []
                for v in variants:
                    variant_wildcards.append({
                        "wildcard": {
                            "tags.raw": {
                                "value": f"*{v}*",  # 前后加 * 实现“包含”逻辑
                                "boost": 1.5
                            }
                        }
                    })
                
                # 将这组同义词的查询打包，只要命中其中任意一个同义词的包含查询即可
                should_clauses.append({
                    "bool": {
                        "should": variant_wildcards,
                        "minimum_should_match": 1
                    }
                })
                
                # # --- 修改结束 ---

                

        # 兜底：如果分词没生成有效子句（防止空查询报错），保留原句简单搜索
        if not should_clauses:
            should_clauses.append({
                 "wildcard": {"tags.raw": {"value": f"*{query_text.strip()}*", "boost": 1.0}}
            })

        # 3. 组装最终查询 [cite: 7, 12, 13]
        body = {
            "query": {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1 # 至少命中一个 Token（或其同义词）
                }
            },
            "from": offset,
            "size": limit
        }
        self._apply_trash_filter(body["query"]["bool"])
        res = self.es.search(index=IMAGE_INDEX, body=body)
        return self._format_es_response(res)


    def browse(self, filter_type, tags, offset, limit, min_tags=None, max_tags=None):
        # 基础查询结构
        query = {"bool": {"must": [], "must_not": []}}
        
        # 1. 现有逻辑：处理 filter_type (保留原逻辑)
        if filter_type == 'untagged':
            query["bool"]["must_not"].append({"exists": {"field": "tags"}})
        elif filter_type == 'tagged':
            query["bool"]["must"].append({"exists": {"field": "tags"}})
            
        # 2. 现有逻辑：处理标签筛选 (保留原逻辑)
        if tags:
            for t in tags:
                variants = self.get_all_variants(t)
                query["bool"]["must"].append({"terms": {"tags.raw": variants}})

        # 3. 新增逻辑：处理标签数量筛选 (Script Query)
        # 如果 min=0 且 max=None(无上限)，则不需要过滤，节省性能
        allow_trash = self._includes_trash_tag(tags)

        if min_tags is not None or max_tags is not None:
            script_source = """
                int count = doc.containsKey('tags.raw') ? doc['tags.raw'].size() : 0;
                return count >= params.min && (params.max == -1 || count <= params.max);
            """
            
            # 处理默认值：前端传 -1 或空表示无上限
            p_min = int(min_tags) if min_tags is not None else 0
            p_max = int(max_tags) if max_tags is not None else -1
            
            # 只有当限制条件有效时才添加 Script
            # (例如：如果 min=0 且 max=-1，其实就是全选，跳过脚本)
            if not (p_min == 0 and p_max == -1):
                query["bool"]["must"].append({
                    "script": {
                        "script": {
                            "source": script_source,
                            "params": {
                                "min": p_min,
                                "max": p_max
                            }
                        }
                    }
                })

        body = {
            "query": query,
            "from": offset,
            "size": limit,
            "sort": [{"filename": "asc"}] 
        }
        self._apply_trash_filter(query["bool"], allow_trash=allow_trash)
        return self._format_es_response(self.es.search(index=IMAGE_INDEX, body=body))

    def _format_es_response(self, res):
        results = []
        for h in res['hits']['hits']:
            src = h['_source']
            results.append(self._build_image_payload(
                src['filename'],
                src.get("tags", []),
                src.get("md5", ""),
                h.get('_score', 0)
            ))
        return {
            "results": results,
            "total": res['hits']['total']['value']
        }

    def check_md5_exists(self, md5_val: str):
        """仅通过 MD5 判断是否已存在（不上传原图）"""
        if not md5_val:
            return {"exists": False, "error": True, "message": "缺少 md5 参数"}

        md5_val = md5_val.lower()
        if self.es.exists(index=IMAGE_INDEX, id=md5_val):
            src = self.es.get(index=IMAGE_INDEX, id=md5_val)['_source']
            payload = self._build_image_payload(src['filename'], src.get('tags', []), md5_val)
            payload["exists"] = True
            payload["message"] = "图片已存在（未重复上传）"
            return payload
        return {"exists": False, "md5": md5_val, "message": "未找到重复图片"}

    def check_upload(self, file_obj, provided_md5=None):
        # 服务器仍然校验 MD5 与客户端声明一致，避免错误命名
        md5_calculated = calculate_md5(file_stream=file_obj)
        file_obj.seek(0)

        if provided_md5 and provided_md5.lower() != md5_calculated:
            return {
                "exists": False,
                "error": True,
                "message": "MD5 校验失败，文件与声明值不一致"
            }

        md5_val = provided_md5.lower() if provided_md5 else md5_calculated

        if self.es.exists(index=IMAGE_INDEX, id=md5_val):
            src = self.es.get(index=IMAGE_INDEX, id=md5_val)['_source']
            payload = self._build_image_payload(src['filename'], src.get('tags', []), md5_val)
            payload["exists"] = True
            payload["message"] = "图片已存在"
            return payload
        
        original_ext = os.path.splitext(file_obj.filename)[1].lower()
        ext = original_ext[1:] if original_ext.startswith('.') else original_ext
        # 统一 jpeg -> jpg
        if ext == 'jpeg':
            ext = 'jpg'
        if not ext or ext not in ALLOWED_EXTS:
            return {
                "exists": False,
                "error": True,
                "message": f"不支持的文件类型，仅支持: {', '.join(sorted(ALLOWED_EXTS))}"
            }
        original_ext = f".{ext}"
        
        new_filename = f"{md5_val}{original_ext}"
        save_path = os.path.join(IMAGE_FOLDER, new_filename)
        file_obj.save(save_path)
        self._ensure_thumbnail_exists(new_filename)
        
        try:
            self.es.index(index=IMAGE_INDEX, id=md5_val, body={
                "filename": new_filename, 
                "tags": [], 
                "md5": md5_val
            })
        except Exception as e:
            print(f"Index error: {e}")
        
        payload = self._build_image_payload(new_filename, [], md5_val)
        payload["exists"] = False
        payload["message"] = "上传成功"
        return payload



    def delete_image_file(self, filename, restore=False):
        rel_path = ((filename or '').replace('\\', '/')).strip('/')
        if not rel_path:
            return False, "缺少图片路径"
        doc_id = os.path.basename(rel_path).rsplit('.', 1)[0]
        if restore:
            if not self._is_trash_path(rel_path):
                return False, "该图片不在回收站"
            prefix = f"{TRASH_SUBDIR}/"
            dst_rel = rel_path[len(prefix):] if rel_path.startswith(prefix) else rel_path
        else:
            if self._is_trash_path(rel_path):
                return False, "该图片已在回收站"
            dst_rel = f"{TRASH_SUBDIR}/{rel_path}" if rel_path else TRASH_SUBDIR
        src_rel = rel_path
        src_abs = self._path_join(IMAGE_FOLDER, src_rel)
        dst_abs = self._path_join(IMAGE_FOLDER, dst_rel)
        if not os.path.exists(src_abs):
            return False, "源文件不存在"
        dst_dir = os.path.dirname(dst_abs)
        if dst_dir and not os.path.exists(dst_dir):
            os.makedirs(dst_dir, exist_ok=True)
        if restore:
            if os.path.exists(dst_abs):
                return False, "目标路径已有文件，请先处理冲突"
        else:
            if os.path.exists(dst_abs):
                try:
                    os.remove(dst_abs)
                except Exception as e:
                    print(f"[Delete Image] 无法提前删除目标文件: {e}")
        try:
            shutil.move(src_abs, dst_abs)
        except Exception as e:
            print(f"[Delete Image] 移动文件失败: {e}")
            return False, str(e)
        self._move_thumbnail_file(src_rel, dst_rel)
        tags = []
        try:
            src_doc = self.es.get(index=IMAGE_INDEX, id=doc_id)['_source']
            tags = src_doc.get('tags', []) or []
        except es_exceptions.NotFoundError:
            tags = []
        except Exception as e:
            print(f"[Delete Image] 读取索引失败: {e}")
        keep_trash = not restore
        new_tags = self._ensure_trash_tag(tags, keep_trash)
        update_doc = {
            "filename": dst_rel,
            "tags": new_tags,
            "md5": doc_id,
        }
        try:
            self.es.update(index=IMAGE_INDEX, id=doc_id, body={"doc": update_doc, "doc_as_upsert": True})
        except Exception as e:
            print(f"[Delete Image] 更新索引失败: {e}")
        message = "已恢复" if restore else "已移入回收站"
        return True, message

    def update_tag_group(self, main_tag, synonyms):
        self.synonym_map[main_tag] = synonyms
        for s in synonyms:
            if s in self.synonym_map: del self.synonym_map[s]
        self._rebuild_reverse_map()
        self._save_metadata()
        return True
  


    def import_json(self, data):
        """
        生成器模式导入：
        支持流式输出消息，以便前端能收到“等待中”和“完成”两次通知
        """
        # 1. 检查锁状态，如果被锁，先发送一条消息
        if self.task_lock.locked():
            print("[Import] 检测到后台忙，发送等待提示...")
            # yield 第一条消息 (NDJSON格式，每行一个JSON)
            yield json.dumps({"status": "waiting", "message": "正在等待后台扫描完成..."}) + "\n"

        # 2. 申请锁 (这步会阻塞，直到后台线程释放锁)
        with self.task_lock:
            print("[Import] 已获得锁，开始导入数据...")
            
            try:
                # --- A. 恢复标签同义词 ---
                if "tag_synonyms" in data:
                    self.synonym_map = data["tag_synonyms"]
                    self._rebuild_reverse_map()
                    self._save_metadata()

                # --- B. 恢复图片索引数据 ---
                if "images" in data:
                    actions = []
                    images_dict = data["images"]
                    print(f"[Import] 正在处理 {len(images_dict)} 条记录...")
                    
                    for old_fname, info in images_dict.items():
                        md5_val = info.get("md5", "")
                        tags = info.get("tags", [])
                        
                        if not md5_val: continue
                        
                        restore_filename = old_fname if old_fname else f"{md5_val}.jpg"
                        
                        action = {
                            "_op_type": "update",
                            "_index": IMAGE_INDEX,
                            "_id": md5_val, 
                            "doc": {
                                "tags": tags,
                                "md5": md5_val
                            },
                            "upsert": {
                                "filename": restore_filename, 
                                "tags": tags,
                                "md5": md5_val
                            }
                        }
                        actions.append(action)
                        
                        if len(actions) >= 1000:
                            helpers.bulk(self.es, actions)
                            actions = []
                    
                    if actions:
                        helpers.bulk(self.es, actions)
                    
                    self.es.indices.refresh(index=IMAGE_INDEX)
                
                msg = f"导入成功！已恢复 {len(data.get('images', {}))} 条记录。"
                print(f"[Import] {msg}")
                
                # yield 第二条消息 (成功)
                yield json.dumps({"status": "success", "message": msg}) + "\n"
                
            except Exception as e:
                traceback.print_exc()
                # yield 错误消息
                yield json.dumps({"status": "error", "message": f"导入失败: {str(e)}"}) + "\n"
            
        # with 块结束，锁会自动释放，后台 sync 线程可以再次运行
            

    def export_json(self):
        try:
            print("[Export] 开始导出数据...")
            export_data = {
                "common_tags": {}, 
                "tag_synonyms": self.synonym_map,
                "images": {}
            }

            scan_gen = helpers.scan(
                self.es, 
                index=IMAGE_INDEX, 
                query={"query": {"match_all": {}}},
                preserve_order=True
            )

            for hit in scan_gen:
                src = hit['_source']
                fname = src['filename']
                export_data["images"][fname] = {
                    "tags": src.get("tags", []),
                    "md5": src.get("md5", "")
                }

            return json.dumps(export_data, ensure_ascii=False, indent=2)
            
        except Exception as e:
            print(f"[Export Error] {e}")
            return json.dumps({"error": str(e)})


# =========================
# 3. Flask App
# =========================

dm = DataManager()
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024

@app.route('/')
def idx(): return send_file('index.html')
@app.route('/script.js')
def js(): return send_file('script.js')
@app.route('/style.css')
def css(): return send_file('style.css')
@app.route('/images/<path:f>')
def img(f): return send_from_directory(IMAGE_FOLDER, f)
@app.route('/thumbnails/<path:f>')
def thumbnail(f): return send_from_directory(THUMBNAIL_FOLDER, f)

# [修改点 3] 新增 favicon 路由
@app.route('/favicon.ico')
def favicon(): return send_file('favicon.ico')

@app.route('/api/get_next_untagged_image')
def next_img(): 
    return jsonify(dm.get_next_untagged_image(request.args.get('current'), request.args.get('filter', 'untagged')))

@app.route('/api/save_tags', methods=['POST'])
def save_t(): return jsonify(dm.save_tags(request.json.get('filename'), request.json.get('tags', [])))

@app.route('/api/search')
def search():
    i = [x.strip() for x in request.args.get('include', '').split(',') if x.strip()]
    e = [x.strip() for x in request.args.get('exclude', '').split(',') if x.strip()]
    return jsonify(dm.search(i, e, request.args.get('offset', 0, int), request.args.get('limit', 50, int)))

@app.route('/api/semantic_search')
def semantic():
    return jsonify(dm.semantic_search(request.args.get('q', ''), request.args.get('offset', 0, int), request.args.get('limit', 50, int)))

@app.route('/api/browse')
def browse():
    t = [x.strip() for x in request.args.get('tag', '').split(',') if x.strip()]
    
    # 获取新增参数
    min_tags = request.args.get('min_tags', type=int) # 默认为 None
    max_tags = request.args.get('max_tags', type=int) # 默认为 None
    
    return jsonify(dm.browse(
        request.args.get('filter', 'all'), 
        t, 
        request.args.get('offset', 0, int), 
        request.args.get('limit', 50, int),
        min_tags=min_tags, # 传入
        max_tags=max_tags  # 传入
    ))

@app.route('/api/get_common_tags')
def common():
    return jsonify(dm.get_common_tags(request.args.get('limit', 100, int), request.args.get('offset', 0, int), request.args.get('query', '')))

@app.route('/api/import_json', methods=['POST'])
def import_data():
    if 'file' not in request.files: 
        return jsonify({"success": False, "message": "No file"}), 400
    
    try:
        # 读取文件内容
        file_content = request.files['file'].read()
        data = json.loads(file_content)
        
        # 返回流式响应
        return Response(
            stream_with_context(dm.import_json(data)),
            mimetype='application/json'
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 400
    
@app.route('/api/delete_image', methods=['POST'])
def del_img():
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename')
    if not filename:
        return jsonify({"success": False, "message": "缺少 filename 参数"}), 400
    restore = bool(data.get('restore'))
    success, message = dm.delete_image_file(filename, restore=restore)
    return jsonify({"success": success, "message": message})

@app.route('/api/check_md5_exists')
def chk_md5():
    return jsonify(dm.check_md5_exists(request.args.get('md5', '').strip()))

@app.route('/api/check_upload', methods=['POST'])
def chk_up(): 
    if 'file' not in request.files: return jsonify({"success": False, "message": "No file part"}), 400
    return jsonify(dm.check_upload(request.files['file'], request.form.get('md5')))

@app.route('/api/add_common_tag', methods=['POST'])
def add_c(): return jsonify({"success": True}) 

@app.route('/api/delete_common_tag', methods=['POST'])
def del_c(): return jsonify({"success": True})

@app.route('/api/update_synonyms', methods=['POST'])
def up_syn(): return jsonify({"success": dm.update_tag_group(request.json.get('main_tag'), request.json.get('synonyms', []))})

@app.route('/api/export_json')
def export(): 
    json_str = dm.export_json()
    return Response(
        json_str, 
        mimetype='application/json', 
        headers={'Content-Disposition': 'attachment;filename=meme_db_md5_backup.json'}
    )

# =========================
# 新增：手动触发扫描路由
# =========================
@app.route('/api/manual_scan')
def manual_scan_api():
    # 1. 校验密钥 (对应脚本中的 key 参数)
    req_key = request.args.get('key')
    if req_key != 'bqbq_secure_scan_key_2025_v1':
        return "Error: 密钥验证失败 (Invalid Key)", 403

    # 2. 检查是否处于维护模式 (防止在导入 JSON 时触发)
    if dm.is_maintenance_mode:
        return "Warning: 系统正在导入数据(维护模式)，请稍后再试。", 503

    # 3. 执行同步逻辑
    try:
        print("[API] 收到手动扫描请求，开始执行...")
        # 直接调用 DataManager 中现有的同步/修复方法
        # 该方法会：扫描磁盘 -> 计算MD5 -> 重命名 -> 增删索引
        dm._sync_files_to_es()
        
        return "Success: 扫描与同步任务已执行完毕 (Sync Completed)."
    except Exception as e:
        traceback.print_exc()
        return f"Error: 执行失败 - {str(e)}", 500
    


@app.route('/api/get_image_info')
def get_image_info():
    filename = request.args.get('filename')
    md5_query = request.args.get('md5')
    
    # 参数校验
    if not filename and not md5_query:
        return jsonify({"success": False, "message": "请提供 filename 或 md5 参数"}), 400

    try:
        src = None

        # 场景 A: 通过 MD5 查询 (最快，MD5 是主键 ID)
        if md5_query:
            try:
                # 尝试直接通过 ID 获取
                res = dm.es.get(index=IMAGE_INDEX, id=md5_query)
                if res.get('found'):
                    src = res['_source']
            except Exception:
                # 如果找不到会抛出 NotFoundError，捕获后 src 仍为 None
                pass

        # 场景 B: 通过 Filename 查询 (如果 MD5 没查到，或者只提供了 filename)
        if not src and filename:
            # filename 在 mapping 中定义为 keyword，使用 term 进行精确匹配
            query_body = {
                "query": {"term": {"filename": filename}},
                "size": 1
            }
            res = dm.es.search(index=IMAGE_INDEX, body=query_body)
            if res['hits']['hits']:
                src = res['hits']['hits'][0]['_source']

        # 结果处理
        if src:
            payload = self._build_image_payload(
                src.get('filename'),
                src.get("tags", []),
                src.get("md5", md5_query if md5_query else "")
            )
            payload["success"] = True
            return jsonify(payload)
        else:
            return jsonify({"success": False, "message": "未找到该图片信息"}), 404

    except Exception as e:
        print(f"[Get Info Error] {e}")
        return jsonify({"success": False, "message": f"服务器内部错误: {str(e)}"}), 500

@app.route('/api/get_by_offset')
def get_by_offset_api():
    # 1. 获取参数
    offset = request.args.get('offset', 0, type=int)
    mode = request.args.get('mode', 'json') # 默认为 json，可选 'image'
    
    try:
        # 2. 直接在此处执行 ES 查询逻辑
        bool_query = {"must": [{"match_all": {}}], "must_not": []}
        self._apply_trash_filter(bool_query)
        body = {
            "from": offset,
            "size": 1,
            "query": {"bool": bool_query},
            "sort": [{"filename": "asc"}]
        }
        
        # 使用全局对象 dm 的 es 客户端进行查询
        res = dm.es.search(index=IMAGE_INDEX, body=body)
        
        # 3. 检查是否查到数据
        if not res['hits']['hits']:
            if mode == 'image':
                return "Image Not Found (Offset out of range)", 404
            return jsonify({"success": False, "message": "偏移量超出范围或库为空"}), 404

        # 4. 提取数据
        src = res['hits']['hits'][0]['_source']
        payload = self._build_image_payload(src['filename'], src.get("tags", []), src.get("md5", ""))
        data = {
            **payload,
            "total": res['hits']['total']['value'], # 返回总数
            "offset": offset
        }

        # 5. 根据模式返回结果
        if mode == 'image':
            # 模式A：图片重定向 -> 浏览器地址栏跳变，显示图片
            return redirect(data['url'])
        else:
            # 模式B：返回 JSON 信息
            return jsonify({"success": True, **data})

    except Exception as e:
        print(f"[Offset Query Error] {e}")
        return jsonify({"success": False, "message": f"Server Error: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(port=5000, debug=True),
