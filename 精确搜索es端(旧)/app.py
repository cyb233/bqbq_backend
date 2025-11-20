# -*- coding: utf-8 -*-
"""
优化后的一体化 Flask 应用
Refactored: 引入双向同义词映射，支持同义词反向搜索，支持修改主标签。
"""
import os
import time
import json
import threading
import traceback
import hashlib
import shutil
from glob import glob
from random import shuffle
from typing import List, Dict, Any, Set
import atexit

from flask import Flask, send_file, send_from_directory, request, jsonify, Response
from werkzeug.utils import secure_filename

# 尝试导入 Elasticsearch
try:
    from elasticsearch import Elasticsearch
    from elasticsearch.exceptions import ConnectionError as ESConnectionError, NotFoundError
except Exception:
    Elasticsearch = None

# =========================
# 1. 配置
# =========================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

IMAGE_FOLDER = os.path.join(BASE_DIR, 'meme_images')
# 【新增】回收站目录
TRASH_DIR = os.path.join(BASE_DIR, 'trash_bin') 

if not os.path.exists(TRASH_DIR):
    os.makedirs(TRASH_DIR, exist_ok=True)
DB_DIR = os.path.join(BASE_DIR, 'db')

ELASTICSEARCH_HOSTS = ['http://localhost:9200']
INDEX_NAME = 'meme_images_index'
COMMON_TAGS_DOC_ID = 'common_tags_store'

PORT = 5000
DEBUG = True
ALLOWED_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}

if not os.path.exists(IMAGE_FOLDER):
    os.makedirs(IMAGE_FOLDER, exist_ok=True)
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR, exist_ok=True)

def calculate_md5(file_path=None, file_stream=None):
    hash_md5 = hashlib.md5()
    if file_path:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
    elif file_stream:
        pos = file_stream.tell()
        for chunk in iter(lambda: file_stream.read(4096), b""):
            hash_md5.update(chunk)
        file_stream.seek(pos)
    return hash_md5.hexdigest()

# =========================
# 2. DataManager 类 (核心重构)
# =========================

class DataManager:
    def __init__(self):
        print("[DataManager] 初始化...")
        self.image_folder = IMAGE_FOLDER

        self._untagged_files: List[str] = []
        self._tagged_images_db: Dict[str, Dict[str, Any]] = {}
        
        # 核心数据结构
        self._common_tags: Dict[str, int] = {} # 记录所有标签(含同义词)的基础引用计数
        self._tag_synonyms_map: Dict[str, List[str]] = {} # Main -> [Synonyms]
        self._synonym_leaf_to_root: Dict[str, str] = {}   # Synonym -> Main (反向索引)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        
        self.use_elasticsearch = False
        self.es = None

        if Elasticsearch is not None:
            try:
                self.es = Elasticsearch(hosts=ELASTICSEARCH_HOSTS)
                if self.es.ping():
                    self.use_elasticsearch = True
                    print("[DataManager] ES 连接成功")
                    self.check_and_create_index()
                else:
                    print("[DataManager] ES Ping 失败")
            except Exception as e:
                print(f"[DataManager] ES 初始化异常: {e}")

        if self.use_elasticsearch:
            self._load_data_from_elasticsearch()
        
        self._initial_scan()
        threading.Thread(target=self._rescan_loop, daemon=True).start()
        atexit.register(self.close)

    def close(self):
        self._stop_event.set()

    # --- 数据同步与索引 ---
    
    def _rebuild_reverse_map(self):
        """重建反向索引 (Synonym -> Main)"""
        self._synonym_leaf_to_root.clear()
        for main_tag, children in self._tag_synonyms_map.items():
            # 主标签自己指向自己 (可选，方便逻辑统一)
            self._synonym_leaf_to_root[main_tag] = main_tag
            for child in children:
                self._synonym_leaf_to_root[child] = main_tag

    def check_and_create_index(self):
        if not self.use_elasticsearch: return
        try:
            if not self.es.indices.exists(index=INDEX_NAME):
                self.es.indices.create(index=INDEX_NAME, body={
                    "mappings": {
                        "properties": {
                            "filename": {"type": "keyword"},
                            "tags": {"type": "keyword"}, # 精确匹配
                            "type": {"type": "keyword"}
                        }
                    }
                })
        except Exception: pass

    def _load_data_from_elasticsearch(self):
        if not self.use_elasticsearch: return
        print("[DataManager] 加载 ES 数据...")
        new_db = {}
        try:
            # 1. 加载元数据 (标签关系)
            try:
                doc = self.es.get(index=INDEX_NAME, id=COMMON_TAGS_DOC_ID)
                src = doc['_source']
                with self._lock:
                    self._common_tags = src.get('common_tags', {})
                    self._tag_synonyms_map = src.get('tag_synonyms', {})
                    self._rebuild_reverse_map()
            except NotFoundError:
                pass

            # 2. 加载图片数据
            res = self.es.search(index=INDEX_NAME, body={"query": {"term": {"type": "image"}}, "size": 10000})
            for hit in res['hits']['hits']:
                src = hit['_source']
                new_db[src['filename']] = {
                    "tags": src.get("tags", []),
                    "md5": src.get("md5", "")
                }
            
            with self._lock:
                self._tagged_images_db = new_db

        except Exception as e:
            print(f"[DataManager] 加载失败: {e}")

    def index_image(self, filename, tags, md5=None):
        if not self.use_elasticsearch: return
        try:
            self.es.index(index=INDEX_NAME, id=filename, body={
                "filename": filename, "tags": tags, "type": "image", "md5": md5
            })
        except Exception: pass

    def index_common_tags(self):
        if not self.use_elasticsearch: return
        try:
            self.es.index(index=INDEX_NAME, id=COMMON_TAGS_DOC_ID, body={
                "common_tags": self._common_tags,
                "tag_synonyms": self._tag_synonyms_map,
                "type": "meta"
            })
        except Exception: pass

    # --- 扫描与文件管理 ---

    def _scan_image_files(self):
        files = set()
        for ext in ALLOWED_EXTS:
            for p in glob(os.path.join(self.image_folder, f'*.{ext}')):
                files.add(os.path.basename(p))
        return files

    def _initial_scan(self):
            # print("[Scan] 开始扫描文件...")
            
            with self._lock:
                # 获取当前磁盘上的所有文件名集合
                disk_files = self._scan_image_files()
                
                # 统计变量
                added_count = 0
                removed_count = 0
                recovered_count = 0 
                md5_fixed_count = 0
                dedup_count = 0 # 去重数量
                
                # --- 1. 预处理：计算所有磁盘文件的 MD5 并分组 ---
                # 格式: { 'md5_val': ['file1.jpg', 'file2.jpg'] }
                md5_groups = {}
                
                # 我们需要遍历 disk_files 来确保每个文件都有 MD5，并按 MD5 分组
                for f in disk_files:
                    full_path = os.path.join(self.image_folder, f)
                    
                    # 初始化 DB 记录
                    if f not in self._tagged_images_db:
                        self._tagged_images_db[f] = {"tags": []}
                        added_count += 1
                    
                    # 补全 MD5
                    if not self._tagged_images_db[f].get('md5'):
                        try:
                            md5_val = calculate_md5(file_path=full_path)
                            self._tagged_images_db[f]['md5'] = md5_val
                            md5_fixed_count += 1
                            if self.use_elasticsearch:
                                self.index_image(f, self._tagged_images_db[f]['tags'], md5=md5_val)
                        except Exception as e:
                            print(f"[Error] 计算 MD5 失败 {f}: {e}")
                            continue
                    
                    # 加入分组
                    current_md5 = self._tagged_images_db[f].get('md5')
                    if current_md5:
                        if current_md5 not in md5_groups:
                            md5_groups[current_md5] = []
                        md5_groups[current_md5].append(f)

                # --- 2. 物理去重：移动重复文件到回收站 ---
                for md5_val, file_list in md5_groups.items():
                    if len(file_list) > 1:
                        # 排序规则：优先保留文件名最短的，长度相同按字母序
                        # 例如: ['a.jpg', 'copy_of_a.jpg'] -> 保留 'a.jpg'
                        file_list.sort(key=lambda x: (len(x), x))
                        
                        keeper = file_list[0]    # 保留这个
                        movers = file_list[1:]   #其他的移走
                        
                        for move_f in movers:
                            src_path = os.path.join(self.image_folder, move_f)
                            dst_path = os.path.join(TRASH_DIR, move_f)
                            
                            try:
                                # 防止回收站里有重名文件覆盖
                                if os.path.exists(dst_path):
                                    name, ext = os.path.splitext(move_f)
                                    dst_path = os.path.join(TRASH_DIR, f"{name}_{int(time.time())}{ext}")
                                    
                                shutil.move(src_path, dst_path)
                                
                                # 【关键】从 disk_files 集合中移除，这样后续逻辑会认为它“丢失”了
                                # 从而触发标签合并逻辑
                                disk_files.remove(move_f)
                                dedup_count += 1
                                print(f"[Dedup] 发现重复，移入回收站: {move_f} (保留: {keeper})")
                            except Exception as e:
                                print(f"[Error] 移动文件失败 {move_f}: {e}")

                # --- 3. 建立“幸存”文件的索引 ---
                # 经过上面的去重，disk_files 里剩下的都是唯一的（或没重复的）
                current_disk_md5_map = {} 
                for f in disk_files:
                    md5 = self._tagged_images_db[f].get('md5')
                    if md5:
                        current_disk_md5_map[md5] = f

                # --- 4. 处理丢失的文件记录 (标签合并与清理) ---
                # 这里会处理刚刚被移入回收站的文件记录
                for db_fname in list(self._tagged_images_db.keys()):
                    
                    # 如果数据库有，磁盘没有 (包含刚刚被移走的文件)
                    if db_fname not in disk_files:
                        
                        missing_record = self._tagged_images_db[db_fname]
                        missing_md5 = missing_record.get('md5')
                        missing_tags = set(missing_record.get('tags', []))
                        
                        # A. 尝试迁移 (合并重复文件的标签)
                        if missing_md5 and missing_md5 in current_disk_md5_map:
                            target_fname = current_disk_md5_map[missing_md5]
                            
                            # 合并标签
                            if missing_tags:
                                current_tags = set(self._tagged_images_db[target_fname].get('tags', []))
                                merged_tags = list(current_tags | missing_tags)
                                
                                # 更新内存
                                self._tagged_images_db[target_fname]['tags'] = merged_tags
                                # 更新 ES
                                if self.use_elasticsearch:
                                    self.index_image(target_fname, merged_tags, md5=missing_md5)
                                
                                print(f"[Recover] 标签合并: {db_fname} -> {target_fname} (标签: {list(missing_tags)})")
                                recovered_count += 1
                            
                            # 删除旧记录
                            del self._tagged_images_db[db_fname]
                            if self.use_elasticsearch:
                                try: self.es.delete(index=INDEX_NAME, id=db_fname)
                                except: pass 
                            
                        # B. 彻底删除
                        else:
                            del self._tagged_images_db[db_fname]
                            if self.use_elasticsearch:
                                try: self.es.delete(index=INDEX_NAME, id=db_fname)
                                except: pass
                            removed_count += 1

                # --- 5. 更新未打标队列 ---
                if any([added_count, removed_count, recovered_count, dedup_count]):
                    self._untagged_files = [
                        f for f, d in self._tagged_images_db.items() 
                        if not d.get("tags")
                    ]
                    # [修改 1]：去掉 shuffle，改为 sort (按文件名排序)
                    #if not hasattr(self, '_shuffled') or not self._shuffled:
                    #    shuffle(self._untagged_files)
                    self._untagged_files.sort() # <--- 改为这行，保证顺序

                # --- 6. 日志 ---
                if any([added_count, removed_count, recovered_count, md5_fixed_count, dedup_count]):
                    print(f"[Scan] 变更汇总: 新增+{added_count}, 物理去重>>{dedup_count}, 标签合并/找回~{recovered_count}, 彻底删除-{removed_count}, 补全MD5 {md5_fixed_count}. 当前总数: {len(self._tagged_images_db)}")

    def _rescan_loop(self):
        while not self._stop_event.is_set():
            time.sleep(15)
            self._initial_scan()

    # --- 核心标签逻辑 (Refactored) ---

    def _get_canonical_tag(self, tag):
        """获取标签的主标签 (如果它是同义词)"""
        return self._synonym_leaf_to_root.get(tag, tag)

    def get_all_variants(self, tag):
        """
        核心查询扩展：输入任意标签，返回其所属组的所有标签
        1. 找到主标签 (Canonical)
        2. 找到该主标签下的所有同义词
        3. 返回 {主标签, 同义词1, 同义词2...}
        """
        root = self._synonym_leaf_to_root.get(tag, tag)
        variants = {root}
        if root in self._tag_synonyms_map:
            variants.update(self._tag_synonyms_map[root])
        return variants

    def update_tag_group(self, new_main_tag, children_tags):
        """
        更新或重构标签组
        :param new_main_tag: 新的主标签
        :param children_tags: 包含的所有同义词列表 (不包括 main_tag)
        """
        new_main = new_main_tag.strip()
        children = list(set([c.strip() for c in children_tags if c.strip() and c.strip() != new_main]))
        
        if not new_main: return False

        with self._lock:
            # 1. 确保新主标签被计数
            if new_main not in self._common_tags:
                self._common_tags[new_main] = 0

            # 2. 清理旧关系：
            # 涉及到的所有标签（新主标签 + 所有子标签）
            involved_tags = [new_main] + children
            
            # 对于每个涉及的标签，如果它之前属于某个组，需要把它从旧组移除
            for t in involved_tags:
                old_root = self._synonym_leaf_to_root.get(t)
                if old_root and old_root in self._tag_synonyms_map:
                    # 如果它是旧组的主标签，整个旧组解散？或者只是把当前t移走？
                    # 策略：如果是重构，通常意味着用户重新定义了关系。
                    # 简单策略：从旧组的 children 列表中移除 t
                    if t in self._tag_synonyms_map[old_root]:
                        self._tag_synonyms_map[old_root].remove(t)
                        # 如果旧组空了，是否删除旧组主键？(保持清洁)
                        if not self._tag_synonyms_map[old_root]:
                             # 保留 old_root 作为独立标签，不删 count
                             pass
                
                # 如果 t 本身就是一个旧的主标签，它的旧子标签怎么办？
                # 策略：这些旧子标签现在“无家可归”，或者应该合并进来？
                # 当前 UI 逻辑是用户在模态框里看到了所有相关词。
                # 如果 t 是旧主标签，我们把它降级。它的旧子标签如果不在此次 children 列表中，
                # 它们将变回独立标签 (因为我们下面会重写 map)。
                if t in self._tag_synonyms_map:
                    # 暂存旧子标签，如果它们不在新的 children 里，它们就独立了
                    orphans = [x for x in self._tag_synonyms_map[t] if x not in involved_tags]
                    # 这里不处理 orphans 自动归并，假设用户在 UI 上操作的是全集
                    del self._tag_synonyms_map[t]

            # 3. 建立新关系
            if children:
                self._tag_synonyms_map[new_main] = children
            
            # 4. 重建反向索引
            self._rebuild_reverse_map()
        
        self.index_common_tags()
        return True

    def get_common_tags(self, limit=100, offset=0, query=""):
        """
        获取常用标签列表。
        逻辑变更：
        1. 只显示主标签（Root Tags）和独立标签。
        2. 不显示已经是同义词（Leaf Tags）的标签。
        3. 排序依据是 (主标签自身计数 + 所有子标签计数)。
        """
        with self._lock:
            # 聚合统计
            aggregated_stats = {} # root -> {count, synonyms}
            
            for tag, count in self._common_tags.items():
                # 找到该标签的归属
                root = self._synonym_leaf_to_root.get(tag, tag)
                
                if root not in aggregated_stats:
                    aggregated_stats[root] = {
                        "tag": root,
                        "count": 0,
                        "synonyms": self._tag_synonyms_map.get(root, [])
                    }
                
                aggregated_stats[root]["count"] += count

            # 转换为列表并过滤
            result_list = []
            q = query.lower().strip()
            
            for root_tag, data in aggregated_stats.items():
                syns = data['synonyms']
                # 搜索过滤：匹配主标签 或 任意同义词
                if q:
                    match_main = q in root_tag.lower()
                    match_syn = any(q in s.lower() for s in syns)
                    if not (match_main or match_syn):
                        continue
                
                result_list.append(data)

            # 排序
            result_list.sort(key=lambda x: x['count'], reverse=True)
            
            total = len(result_list)
            sliced = result_list[offset : offset + limit]
            
            return {"tags": sliced, "total": total}

    # --- 业务 API ---
        #在 DataManager 类中添加删除方法
    def delete_image_file(self, filename):
        with self._lock:
            # 检查文件是否存在
            if filename not in self._tagged_images_db:
                return False, "文件不存在"
            
            # 移动到回收站
            src_path = os.path.join(self.image_folder, filename)
            if os.path.exists(src_path):
                try:
                    dst_path = os.path.join(TRASH_DIR, filename)
                    # 防止回收站重名覆盖
                    if os.path.exists(dst_path):
                        name, ext = os.path.splitext(filename)
                        dst_path = os.path.join(TRASH_DIR, f"{name}_{int(time.time())}{ext}")
                    shutil.move(src_path, dst_path)
                except Exception as e:
                    return False, f"移动文件失败: {e}"
            
            # 删除数据库记录
            del self._tagged_images_db[filename]
            
            # 删除 ES 记录
            if self.use_elasticsearch:
                try:
                    self.es.delete(index=INDEX_NAME, id=filename)
                except Exception:
                    pass
            
            # 如果在未打标队列里，也删掉
            if filename in self._untagged_files:
                self._untagged_files.remove(filename)
                
            return True, "删除成功"
            
            
    # 修改定义：增加 filter_type='untagged' 参数
    def get_next_untagged_image(self, current_filename=None, filter_type='untagged'):
        with self._lock:
            # =================================================
            # 模式 A: 纯未打标模式 (Untagged)
            # =================================================
            if filter_type == 'untagged':
                # --- 1. 队列自清洗 (保留原有功能) ---
                # 清理列表头部无效数据（已打标或不存在的文件）
                while self._untagged_files:
                    head = self._untagged_files[0]
                    if head not in self._tagged_images_db or self._tagged_images_db[head].get("tags"):
                        self._untagged_files.pop(0)
                        continue
                    break 
                
                # --- 2. 自动纠错 (修复隐身 Bug) ---
                # 如果队列空了，最后再全库扫描一次，防止因为保存逻辑漏洞导致有图但没在队列里
                if not self._untagged_files:
                    self._untagged_files = sorted([
                        f for f, d in self._tagged_images_db.items() 
                        if not d.get("tags")
                    ])

                # --- 3. 返回结果 ---
                if self._untagged_files:
                    target_file = self._untagged_files[0] # 默认第一张
                    
                    # 如果有 current_filename，尝试找它的下一张
                    if current_filename:
                        try:
                            curr_idx = self._untagged_files.index(current_filename)
                            if curr_idx + 1 < len(self._untagged_files):
                                target_file = self._untagged_files[curr_idx + 1]
                            else:
                                target_file = self._untagged_files[0] # 循环到头
                        except ValueError:
                            # current 不在队列中（可能刚被标完移除了），保持默认第一张
                            pass

                    md5_val = self._tagged_images_db[target_file].get('md5', '')
                    return {
                        "success": True, 
                        "filename": target_file, 
                        "url": f"/images/{target_file}",
                        "tags": [], 
                        "md5": md5_val,
                        "is_review": False
                    }
                else:
                    # 真的没有未打标图片了
                    return {"success": False, "message": "所有图片均已打标"}

            # =================================================
            # 模式 B: 浏览/复习模式 (All / Tagged)
            # =================================================
            else:
                all_files = sorted(list(self._tagged_images_db.keys()))
                target_list = []
                
                # 根据筛选类型构建列表
                if filter_type == 'tagged':
                    target_list = [f for f in all_files if self._tagged_images_db[f].get("tags")]
                else: # filter_type == 'all'
                    target_list = all_files
                
                if not target_list:
                    return {"success": False, "message": "暂无符合条件的数据"}

                # 寻找下一张 (基于 current_filename)
                next_index = 0
                if current_filename and current_filename in target_list:
                    try:
                        curr_idx = target_list.index(current_filename)
                        next_index = curr_idx + 1
                        if next_index >= len(target_list):
                            next_index = 0 # 循环
                    except ValueError:
                        next_index = 0
                
                target_file = target_list[next_index]
                data = self._tagged_images_db[target_file]
                
                return {
                    "success": True,
                    "filename": target_file,
                    "url": f"/images/{target_file}",
                    "tags": data.get("tags", []),
                    "md5": data.get("md5", ""),
                    "is_review": True,
                    "message": f"{filter_type} View: {next_index + 1}/{len(target_list)}"
                }

    def check_upload(self, file_obj):
        md5_val = calculate_md5(file_stream=file_obj)
        existing_filename = None
        existing_tags = []

        # 查重逻辑
        if self.use_elasticsearch:
            try:
                res = self.es.search(index=INDEX_NAME, body={"query": {"term": {"md5": md5_val}}})
                if res['hits']['hits']:
                    src = res['hits']['hits'][0]['_source']
                    existing_filename = src['filename']
                    existing_tags = src.get('tags', [])
            except: pass
        
        if not existing_filename:
            with self._lock:
                for fname, data in self._tagged_images_db.items():
                    if data.get('md5') == md5_val:
                        existing_filename = fname
                        existing_tags = data.get('tags', [])
                        break

        if existing_filename:
            return {
                "exists": True, 
                "filename": existing_filename, 
                "tags": existing_tags, 
                "url": f"/images/{existing_filename}", 
                "md5": md5_val,                      # <--- 新增：返回 MD5
                "message": "图片已存在，加载标签编辑"   # <--- 修改：更新提示语
            }
        
        # 保存新图
        filename = secure_filename(file_obj.filename) or f"upload_{int(time.time())}.jpg"
        save_path = os.path.join(self.image_folder, filename)
        counter = 1
        while os.path.exists(save_path):
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{counter}{ext}"
            save_path = os.path.join(self.image_folder, filename)
            counter += 1
            
        file_obj.save(save_path)
        with self._lock:
            self._tagged_images_db[filename] = {"tags": [], "md5": md5_val}
            self._untagged_files.append(filename)
        self.index_image(filename, [], md5=md5_val)
        
        return {
            "exists": False, 
            "filename": filename, 
            "tags": [], 
            "url": f"/images/{filename}", 
            "md5": md5_val,      # <--- 新增：返回 MD5
            "message": "上传成功"
        }
    
    def save_tags(self, filename, tags):
            cleaned = sorted(list(set([t.strip() for t in tags if t.strip()])))
            with self._lock:
                if filename in self._tagged_images_db:
                    self._tagged_images_db[filename]["tags"] = cleaned
                    md5_val = self._tagged_images_db[filename].get("md5")
                    
                    # [修复] 逻辑：有标签移出队列，无标签加回队列
                    if cleaned and filename in self._untagged_files:
                        self._untagged_files.remove(filename)
                    elif not cleaned and filename not in self._untagged_files:
                        self._untagged_files.append(filename)
                        self._untagged_files.sort()

                    # ...后续统计和索引代码保持不变...
                    for t in cleaned:
                        self._common_tags[t] = self._common_tags.get(t, 0) + 1
                    self.index_image(filename, cleaned, md5=md5_val)
                    self.index_common_tags()
                    return {"success": True}
            return {"success": False}

    def add_common_tag(self, tag):
        t = tag.strip()
        if not t: return False
        with self._lock:
            self._common_tags[t] = self._common_tags.get(t, 0) + 1
        self.index_common_tags()
        return True
    
    def delete_common_tag(self, tag):
        # 删除逻辑：如果它是Main，解散组；如果它是Leaf，移除关系
        with self._lock:
            if tag in self._common_tags: del self._common_tags[tag]
            # 解除关系
            self.update_tag_group(tag, []) # 作为一个孤立标签（如果它之前是Main，这会清空子集；如果它是子，这没啥用）
            # 更彻底的清理
            if tag in self._tag_synonyms_map:
                del self._tag_synonyms_map[tag]
            
            # 如果它是子标签，找到父标签并移除
            root = self._synonym_leaf_to_root.get(tag)
            if root and root != tag and root in self._tag_synonyms_map:
                if tag in self._tag_synonyms_map[root]:
                    self._tag_synonyms_map[root].remove(tag)
            
            self._rebuild_reverse_map()
        self.index_common_tags()
        return True

    # --- 搜索与浏览 ---

    def _build_query_body(self, filter_type, include, exclude):
        body = {
            "query": {
                "bool": {
                    "must": [{"term": {"type": "image"}}],
                    "must_not": []
                }
            }
        }
        # 包含逻辑：(Tag1 OR Var1 OR Var2) AND (Tag2 OR ...)
        for t in include:
            variants = list(self.get_all_variants(t))
            body["query"]["bool"]["must"].append({"terms": {"tags": variants}})
        
        # 排除逻辑：Exclude T => Exclude T and all its variants
        exclude_flat = []
        for t in exclude:
            exclude_flat.extend(list(self.get_all_variants(t)))
        if exclude_flat:
            body["query"]["bool"]["must_not"].append({"terms": {"tags": exclude_flat}})

        if filter_type == 'untagged':
            body["query"]["bool"]["must_not"].append({"exists": {"field": "tags"}})
        elif filter_type == 'tagged':
            body["query"]["bool"]["must"].append({"exists": {"field": "tags"}})
            
        return body

    def search(self, include, exclude, offset, limit):
        if self.use_elasticsearch:
            body = self._build_query_body("all", include, exclude)
            body["from"] = offset
            body["size"] = limit
            body["sort"] = [{"_score": "desc"}]
            try:
                res = self.es.search(index=INDEX_NAME, body=body)
                return {
                    "results": [{
                        "filename": h['_source']['filename'],
                        "tags": h['_source'].get("tags", []),
                        "url": f"/images/{h['_source']['filename']}",
                        "score": h['_score'],
                        "md5": h['_source'].get("md5", "") # [修改] 添加这行
                    } for h in res['hits']['hits']],
                    "total": res['hits']['total']['value']
                }
            except Exception: return {"results": [], "total": 0}
        
        # Memory Search
        # 预计算 exclude set
        ex_set = set()
        for t in exclude: ex_set.update(self.get_all_variants(t))
        
        res = []
        with self._lock:
            for fname, data in self._tagged_images_db.items():
                img_tags = set(data.get("tags", []))
                if not img_tags.isdisjoint(ex_set): continue
                
                match_count = 0
                match_all = True
                for req in include:
                    variants = self.get_all_variants(req)
                    if img_tags.isdisjoint(variants):
                        match_all = False
                        break
                    match_count += 1
                
                if include and not match_all: continue
                
                res.append({
                    "filename": fname, "tags": list(img_tags), 
                    "url": f"/images/{fname}", 
                    "score": float(match_count if include else 1),
                    "md5": data.get("md5", "") # [修改] 添加这行
                })
        
        res.sort(key=lambda x: x['score'], reverse=True)
        return {"results": res[offset:offset+limit], "total": len(res)}

    def browse(self, filter_type, tags, offset, limit):
        if self.use_elasticsearch:
            body = self._build_query_body(filter_type, tags, [])
            body["from"] = offset
            body["size"] = limit
            body["sort"] = [{"filename": "asc"}]
            try:
                res = self.es.search(index=INDEX_NAME, body=body)
                return {
                    "results": [{"filename": h['_source']['filename'], "tags": h['_source'].get("tags", []), "url": f"/images/{h['_source']['filename']}", "md5": h['_source'].get("md5", "")} for h in res['hits']['hits']],
                    "total": res['hits']['total']['value']
                }
            except Exception: return {"results": [], "total": 0}

        # Memory Browse
        res = []
        with self._lock:
            for fname, data in self._tagged_images_db.items():
                img_tags = set(data.get("tags", []))
                
                if filter_type == 'untagged' and img_tags: continue
                if filter_type == 'tagged' and not img_tags: continue
                
                if tags:
                    match_all = True
                    for req in tags:
                        if img_tags.isdisjoint(self.get_all_variants(req)):
                            match_all = False; break
                    if not match_all: continue
                
                res.append({"filename": fname, "tags": list(img_tags), "url": f"/images/{fname}", "md5": data.get("md5", "")})
                
        res.sort(key=lambda x: x['filename'])
        return {"results": res[offset:offset+limit], "total": len(res)}

    # --- IO ---
    def export_json(self):
        with self._lock:
            return json.dumps({
                "images": self._tagged_images_db,
                "common_tags": self._common_tags,
                "tag_synonyms": self._tag_synonyms_map
            }, ensure_ascii=False, indent=2)

    def import_json(self, data):
        try:
            with self._lock:
                if "images" in data: self._tagged_images_db.update(data["images"])
                if "common_tags" in data: self._common_tags.update(data["common_tags"])
                if "tag_synonyms" in data: self._tag_synonyms_map.update(data["tag_synonyms"])
                self._rebuild_reverse_map()
            
            if self.use_elasticsearch:
                self.index_common_tags()
                for f, d in self._tagged_images_db.items():
                    self.index_image(f, d.get("tags", []), md5=d.get("md5"))
            return True, f"导入成功: {len(data.get('images', {}))} 图片"
        except Exception as e: return False, str(e)


dm = DataManager()
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

@app.route('/')
def idx(): return send_file('index.html')
@app.route('/script.js')
def js(): return send_file('script.js')
@app.route('/style.css')
def css(): return send_file('style.css')

# === 在这里添加 favicon 路由 ===
@app.route('/favicon.ico')
def favicon(): return send_file('favicon.ico')
# ==============================

@app.route('/images/<path:f>')
def img(f): return send_from_directory(IMAGE_FOLDER, f)

@app.route('/api/delete_image', methods=['POST'])
def delete_image():
    return jsonify({"success": dm.delete_image_file(request.json.get('filename'))[0]})

@app.route('/api/get_next_untagged_image')
def next_img(): 
    current = request.args.get('current')
    filter_t = request.args.get('filter', 'untagged')
    
    # 这里的调用必须和上面的定义匹配：
    return jsonify(dm.get_next_untagged_image(current_filename=current, filter_type=filter_t))

@app.route('/api/check_upload', methods=['POST'])
def check_up(): return jsonify(dm.check_upload(request.files.get('file')))

@app.route('/api/save_tags', methods=['POST'])
def save_t(): return jsonify(dm.save_tags(request.json.get('filename'), request.json.get('tags', [])))

@app.route('/api/get_common_tags')
def get_common(): 
    return jsonify(dm.get_common_tags(request.args.get('limit', 100, int), request.args.get('offset', 0, int), request.args.get('query', '')))

@app.route('/api/add_common_tag', methods=['POST'])
def add_common(): return jsonify({"success": dm.add_common_tag(request.json.get('tag', ''))})

@app.route('/api/delete_common_tag', methods=['POST'])
def del_common(): return jsonify({"success": dm.delete_common_tag(request.json.get('tag', ''))})

@app.route('/api/update_synonyms', methods=['POST'])
def update_syn():
    d = request.json
    return jsonify({"success": dm.update_tag_group(d.get('main_tag'), d.get('synonyms', []))})

@app.route('/api/browse')
def browse():
    t = [x.strip() for x in request.args.get('tag', '').split(',') if x.strip()]
    return jsonify(dm.browse(request.args.get('filter', 'all'), t, request.args.get('offset', 0, int), request.args.get('limit', 50, int)))

@app.route('/api/search')
def search():
    i = [x.strip() for x in request.args.get('include', '').split(',') if x.strip()]
    e = [x.strip() for x in request.args.get('exclude', '').split(',') if x.strip()]
    return jsonify(dm.search(i, e, request.args.get('offset', 0, int), request.args.get('limit', 50, int)))

@app.route('/api/export_json')
def export(): return Response(dm.export_json(), mimetype='application/json', headers={'Content-Disposition': 'attachment;filename=meme_db.json'})

@app.route('/api/import_json', methods=['POST'])
def import_data():
    if 'file' not in request.files: return jsonify({"success": False}), 400
    try: return jsonify({"success": True, "message": dm.import_json(json.load(request.files['file']))[1]})
    except Exception as e: return jsonify({"success": False, "message": str(e)}), 400

if __name__ == '__main__':
    app.run(port=PORT, debug=DEBUG)