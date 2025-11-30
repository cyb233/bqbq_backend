# -*- coding: utf-8 -*-
import os
import json
import hashlib
import shutil
import threading
import traceback
import concurrent.futures
import random
import sqlite3
from typing import List, Dict, Any, Set

# 新增依赖: pip install jieba
import jieba

from flask import Flask, send_file, send_from_directory, request, jsonify, Response, stream_with_context, redirect
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
DB_PATH = os.path.join(DB_DIR, 'meme.db') # SQLite 数据库文件路径
THUMBNAIL_MAX_SIZE = 600

os.makedirs(IMAGE_FOLDER, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

ALLOWED_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}

# 初始化 Jieba 分词 (首次运行会加载字典)
jieba.initialize()

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
# 2. Database Helper
# =========================

class DBHandler:
    """处理 SQLite 连接与基础操作"""
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_tables()

    def get_conn(self):
        """获取数据库连接，配置 row_factory"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 开启 WAL 模式提高并发性能
        conn.execute("PRAGMA journal_mode=WAL;") 
        return conn

    def _init_tables(self):
        conn = self.get_conn()
        cursor = conn.cursor()
        
        # 1. 图片主表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                md5 TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 创建 filename 索引加速排序
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_filename ON images (filename)')

        # 2. 标签表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        ''')
        # 创建 name 索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tag_name ON tags (name)')

        # 3. 图片-标签关联表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS image_tags (
                image_md5 TEXT,
                tag_id INTEGER,
                PRIMARY KEY (image_md5, tag_id),
                FOREIGN KEY (image_md5) REFERENCES images(md5) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        ''')
        
        # 4. 同义词表 (扁平化存储: main_tag -> synonym)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS synonyms (
                main_tag TEXT,
                synonym TEXT,
                PRIMARY KEY (main_tag, synonym)
            )
        ''')
        
        conn.commit()
        conn.close()

# =========================
# 3. DataManager (Core)
# =========================

class DataManager:
    def __init__(self):
        print("[DataManager] 初始化 (SQLite 版)...")
        self.db = DBHandler(DB_PATH)
        
        # 任务锁：用于互斥“后台自动扫描”和“手动导入/导出”
        self.task_lock = threading.Lock()
        
        # 内存缓存同义词，减少数据库查询
        self.synonym_map = {} # {main: [syn1, syn2]}
        self.synonym_leaf_to_root = {} # {syn1: main, main: main}
        self._load_metadata()

        # 启动后台任务
        threading.Thread(target=self._generate_missing_thumbnails, daemon=True).start()
        threading.Thread(target=self._sync_files_to_db, daemon=True).start()

    def _load_metadata(self):
        """从 SQLite 加载同义词到内存"""
        conn = self.db.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT main_tag, synonym FROM synonyms")
        rows = cursor.fetchall()
        conn.close()

        self.synonym_map = {}
        self.synonym_leaf_to_root = {}
        
        for r in rows:
            main, syn = r['main_tag'], r['synonym']
            if main not in self.synonym_map:
                self.synonym_map[main] = []
            self.synonym_map[main].append(syn)
            self.synonym_leaf_to_root[syn] = main
            self.synonym_leaf_to_root[main] = main # 根也是自己的根

    def _save_synonyms(self, new_map):
        """保存同义词：全量覆盖逻辑"""
        conn = self.db.get_conn()
        try:
            conn.execute("DELETE FROM synonyms")
            data_to_insert = []
            for main, syns in new_map.items():
                # 确保存入根节点自己（虽然逻辑上不需要，但为了完整性）
                for s in syns:
                    data_to_insert.append((main, s))
            
            if data_to_insert:
                conn.executemany("INSERT OR IGNORE INTO synonyms (main_tag, synonym) VALUES (?, ?)", data_to_insert)
            conn.commit()
            
            # 更新内存
            self.synonym_map = new_map
            self._rebuild_reverse_map()
        except Exception as e:
            print(f"Error saving synonyms: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _rebuild_reverse_map(self):
        self.synonym_leaf_to_root = {}
        for main, children in self.synonym_map.items():
            self.synonym_leaf_to_root[main] = main
            for child in children:
                self.synonym_leaf_to_root[child] = main

    # --- 辅助工具 ---
    def _path_join(self, base_dir, relative_path):
        return os.path.join(base_dir, *relative_path.split('/'))

    def _thumbnail_rel_path(self, filename):
        base, _ = os.path.splitext(filename)
        return f"{base}_thumbnail.jpg"
    
    def _normalize_rel_path(self, rel_path):
        if not rel_path: return ''
        return rel_path.replace('\\', '/').strip('/')

    def _is_trash_path(self, rel_path):
        if not rel_path: return False
        return self._normalize_rel_path(rel_path).startswith(f"{TRASH_SUBDIR}/")

    def _get_tags_for_image(self, conn, md5):
        """获取单张图片的 tags 列表"""
        cursor = conn.cursor()
        cursor.execute('''
            SELECT t.name FROM tags t
            JOIN image_tags it ON t.id = it.tag_id
            WHERE it.image_md5 = ?
        ''', (md5,))
        return [r['name'] for r in cursor.fetchall()]

    def _build_image_payload(self, filename, tags=None, md5=None, score=None):
        thumb_rel = self._ensure_thumbnail_exists(filename)
        thumb_url = f"/thumbnails/{thumb_rel}" if thumb_rel else None
        
        # 如果 tags 为 None，尝试懒加载（视情况而定，这里假设通常传入了 tags）
        if tags is None and md5:
            # 注意：在循环中慎用，会产生 N+1 查询
            pass 

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

    # --- 缩略图逻辑 (保持原样) ---
    def _extract_random_frame(self, img: Image.Image):
        frames = getattr(img, "n_frames", 1)
        if frames > 1:
            try: img.seek(random.randrange(frames))
            except: img.seek(0)
        return img.copy()

    def _create_thumbnail_file(self, source_path, thumb_path):
        with Image.open(source_path) as img:
            frame = self._extract_random_frame(img)
            if frame.mode not in ("RGB", "L"): frame = frame.convert("RGB")
            elif frame.mode == "L": frame = frame.convert("RGB")
            frame.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.LANCZOS)
            os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
            frame.save(thumb_path, "JPEG", quality=85, optimize=True)

    def _ensure_thumbnail_exists(self, filename):
        if not filename: return None
        thumb_rel = self._thumbnail_rel_path(filename)
        thumb_full = self._path_join(THUMBNAIL_FOLDER, thumb_rel)
        src_full = self._path_join(IMAGE_FOLDER, filename)
        try:
            if not os.path.exists(src_full): return None
            if os.path.exists(thumb_full): return thumb_rel
            self._create_thumbnail_file(src_full, thumb_full)
            return thumb_rel
        except Exception as e:
            print(f"[Thumb Error] {thumb_rel}: {e}")
            return None

    def _generate_missing_thumbnails(self):
        print("[Thumb] 开始检查缩略图...")
        for root, _, files in os.walk(IMAGE_FOLDER):
            for fname in files:
                if fname.rsplit('.', 1)[-1].lower() in ALLOWED_EXTS:
                    rel = os.path.relpath(os.path.join(root, fname), IMAGE_FOLDER).replace('\\', '/')
                    self._ensure_thumbnail_exists(rel)
        print("[Thumb] 完成")

    # --- 核心同步逻辑 (DB版) ---

    def _process_file_standardization(self, file_path):
        """计算MD5并重命名"""
        try:
            dir_path = os.path.dirname(file_path)
            fname = os.path.basename(file_path)
            if '.' not in fname: return None
            ext = fname.rsplit('.', 1)[1].lower()
            if ext not in ALLOWED_EXTS: return None

            md5_val = calculate_md5(file_path=file_path)
            expected_name = f"{md5_val}.{ext}"
            final_name_in_dir = expected_name
            
            if fname != expected_name:
                new_full_path = os.path.join(dir_path, expected_name)
                try:
                    if not os.path.exists(new_full_path):
                        os.rename(file_path, new_full_path)
                    else:
                        os.remove(file_path) # 重复文件删除
                except OSError:
                    return None 
            
            abs_final_path = os.path.join(dir_path, final_name_in_dir)
            rel_path = os.path.relpath(abs_final_path, IMAGE_FOLDER).replace('\\', '/')
            return (md5_val, rel_path)
        except Exception:
            traceback.print_exc()
            return None

    def _sync_files_to_db(self):
        """后台同步：磁盘文件 <-> SQLite"""
        with self.task_lock:
            print("[Sync] 开始同步...")
            conn = self.db.get_conn()
            try:
                # 1. 获取 DB 现状
                db_files = {} # md5 -> filename
                cursor = conn.cursor()
                cursor.execute("SELECT md5, filename FROM images")
                for row in cursor.fetchall():
                    db_files[row['md5']] = row['filename']
                
                # 2. 扫描磁盘
                disk_md5_map = {} # md5 -> rel_path
                files_to_process = []
                
                if os.path.exists(IMAGE_FOLDER):
                    for root, dirs, files in os.walk(IMAGE_FOLDER):
                        for fname in files:
                            full_path = os.path.join(root, fname)
                            fname_base = os.path.basename(fname)
                            fname_stem = fname_base.rsplit('.', 1)[0]
                            
                            # 假设文件名是 MD5 (快速检查)
                            if fname_stem in db_files:
                                rel = os.path.relpath(full_path, IMAGE_FOLDER).replace('\\', '/')
                                disk_md5_map[fname_stem] = rel
                            else:
                                files_to_process.append(full_path)

                # 3. 处理未知/改名文件
                if files_to_process:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        future_to_file = {executor.submit(self._process_file_standardization, f): f for f in files_to_process}
                        for future in concurrent.futures.as_completed(future_to_file):
                            res = future.result()
                            if res:
                                md5_val, rel_path = res
                                disk_md5_map[md5_val] = rel_path

                # 4. 计算差异
                disk_ids = set(disk_md5_map.keys())
                db_ids = set(db_files.keys())
                
                to_add = disk_ids - db_ids
                to_delete = db_ids - disk_ids
                to_update_path = [] # (md5, new_path)
                
                # 检查路径变化
                for mid in disk_ids & db_ids:
                    if db_files[mid] != disk_md5_map[mid]:
                        to_update_path.append((mid, disk_md5_map[mid]))
                        # 顺便检查是否需要加/减 trash 标签
                        new_is_trash = self._is_trash_path(disk_md5_map[mid])
                        # 获取旧 tag，这一步比较耗时，但为了准确性需要做
                        # 优化：可以稍后在业务逻辑中 lazy update，或者这里统一 update
                        # 这里简化处理：如果是路径变动，强制根据路径刷新 trash 标签状态
                        self._sync_trash_tag_in_db(conn, mid, new_is_trash)

                # 5. 执行 DB 变更
                if to_delete:
                    print(f"[Sync] 删除失效记录: {len(to_delete)}")
                    conn.executemany("DELETE FROM images WHERE md5 = ?", [(x,) for x in to_delete])
                    # image_tags 会因为 DELETE CASCADE 自动清理
                
                if to_update_path:
                    print(f"[Sync] 更新路径: {len(to_update_path)}")
                    for mid, new_p in to_update_path:
                        conn.execute("UPDATE images SET filename = ? WHERE md5 = ?", (new_p, mid))
                        
                if to_add:
                    print(f"[Sync] 新增记录: {len(to_add)}")
                    for mid in to_add:
                        rel = disk_md5_map[mid]
                        conn.execute("INSERT OR IGNORE INTO images (md5, filename) VALUES (?, ?)", (mid, rel))
                        self._ensure_thumbnail_exists(rel)
                        # 初始化 trash 标签
                        if self._is_trash_path(rel):
                            self._add_tag_to_image(conn, mid, TRASH_TAG)

                conn.commit()
                print("[Sync] 同步完成")

            except Exception as e:
                print(f"[Sync Error] {e}")
                traceback.print_exc()
            finally:
                conn.close()

    def _sync_trash_tag_in_db(self, conn, md5, should_be_trash):
        """确保 DB 中的标签与物理路径一致"""
        # 1. 查找 tag_id
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tags WHERE name = ?", (TRASH_TAG,))
        row = cursor.fetchone()
        trash_tag_id = row['id'] if row else None

        if should_be_trash:
            if not trash_tag_id:
                cursor.execute("INSERT INTO tags (name) VALUES (?)", (TRASH_TAG,))
                trash_tag_id = cursor.lastrowid
            cursor.execute("INSERT OR IGNORE INTO image_tags (image_md5, tag_id) VALUES (?, ?)", (md5, trash_tag_id))
        else:
            if trash_tag_id:
                cursor.execute("DELETE FROM image_tags WHERE image_md5 = ? AND tag_id = ?", (md5, trash_tag_id))

    def _add_tag_to_image(self, conn, md5, tag_name):
        cursor = conn.cursor()
        # 确保 tag 存在
        cursor.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
        # 获取 tag id (如果是刚插入的，需要 select)
        cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
        tag_id = cursor.fetchone()['id']
        # 插入关联
        cursor.execute("INSERT OR IGNORE INTO image_tags (image_md5, tag_id) VALUES (?, ?)", (md5, tag_id))


    # --- 业务逻辑 ---

    def get_all_variants(self, tag):
        root = self.synonym_leaf_to_root.get(tag, tag)
        variants = {root}
        if root in self.synonym_map:
            variants.update(self.synonym_map[root])
        return list(variants)

    def get_next_untagged_image(self, current_filename=None, filter_type='untagged'):
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        # 基础 SQL：找出所有有关联标签的图片 MD5
        # 优化：filter_type logic
        
        # 排除 trash 的图片
        trash_exclude_clause = f"""
            md5 NOT IN (
                SELECT image_md5 FROM image_tags 
                JOIN tags ON image_tags.tag_id = tags.id 
                WHERE tags.name = '{TRASH_TAG}'
            )
        """

        where_clauses = [trash_exclude_clause]
        
        if filter_type == 'untagged':
            # 没有标签 (left join image_tags is null)
            where_clauses.append("md5 NOT IN (SELECT DISTINCT image_md5 FROM image_tags)")
        elif filter_type == 'tagged':
            where_clauses.append("md5 IN (SELECT DISTINCT image_md5 FROM image_tags)")
        
        if current_filename:
            where_clauses.append("filename > ?")
            params = [current_filename]
        else:
            params = []

        query = f"SELECT * FROM images WHERE {' AND '.join(where_clauses)} ORDER BY filename ASC LIMIT 1"
        
        cursor.execute(query, params)
        row = cursor.fetchone()
        
        # 如果到底了，循环回到头部
        if not row and current_filename:
            # 去掉 filename > ? 条件重查
            base_clauses = where_clauses[:-1] 
            query_loop = f"SELECT * FROM images WHERE {' AND '.join(base_clauses)} ORDER BY filename ASC LIMIT 1"
            cursor.execute(query_loop)
            row = cursor.fetchone()
            if row:
                tags = self._get_tags_for_image(conn, row['md5'])
                return {"success": True, "message": "循环回到第一张", **self._build_image_payload(row['filename'], tags, row['md5'])}
        
        if row:
            tags = self._get_tags_for_image(conn, row['md5'])
            conn.close()
            return {"success": True, **self._build_image_payload(row['filename'], tags, row['md5'])}
        
        conn.close()
        return {"success": False, "message": "没有更多图片了"}

    def save_tags(self, filename, tags):
        if not filename: return {"success": False, "message": "No filename"}
        normalized_filename = self._normalize_rel_path(filename)
        md5_id = os.path.basename(normalized_filename).rsplit('.', 1)[0]
        
        tag_candidates = tags if isinstance(tags, list) else []
        cleaned_tags = sorted({str(t).strip() for t in tag_candidates if str(t).strip()})
        should_be_trash = TRASH_TAG in cleaned_tags
        
        conn = self.db.get_conn()
        try:
            # 1. 检查图片是否存在
            cursor = conn.cursor()
            cursor.execute("SELECT filename FROM images WHERE md5 = ?", (md5_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "message": "Image not found in DB"}
            
            current_path = row['filename']
            currently_in_trash = self._is_trash_path(current_path)
            
            # 2. 处理物理文件移动
            final_path = current_path
            action_msg = "标签已保存"
            
            if should_be_trash and not currently_in_trash:
                # 移入垃圾箱
                new_rel = f"{TRASH_SUBDIR}/{os.path.basename(current_path)}"
                final_path = self._relocate_image(current_path, new_rel)
                action_msg = "已移入回收站"
            elif not should_be_trash and currently_in_trash:
                # 移出垃圾箱 (移到根目录)
                new_rel = os.path.basename(current_path)
                final_path = self._relocate_image(current_path, new_rel)
                action_msg = "已恢复"

            # 3. 更新 images 表路径
            if final_path != current_path:
                cursor.execute("UPDATE images SET filename = ? WHERE md5 = ?", (final_path, md5_id))
            
            # 4. 更新标签 (全量替换)
            # a. 获取所有目标 tag 的 id (如果不存在则创建)
            tag_ids = []
            for t in cleaned_tags:
                cursor.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                cursor.execute("SELECT id FROM tags WHERE name = ?", (t,))
                tag_ids.append(cursor.fetchone()['id'])
            
            # b. 删除旧关联
            cursor.execute("DELETE FROM image_tags WHERE image_md5 = ?", (md5_id,))
            
            # c. 插入新关联
            if tag_ids:
                cursor.executemany("INSERT INTO image_tags (image_md5, tag_id) VALUES (?, ?)", 
                                   [(md5_id, tid) for tid in tag_ids])
            
            conn.commit()
            return {"success": True, "message": action_msg}
            
        except Exception as e:
            conn.rollback()
            traceback.print_exc()
            return {"success": False, "message": str(e)}
        finally:
            conn.close()

    def _relocate_image(self, src_rel, dst_rel):
        """移动物理文件和缩略图"""
        src_full = self._path_join(IMAGE_FOLDER, src_rel)
        dst_full = self._path_join(IMAGE_FOLDER, dst_rel)
        
        if os.path.abspath(src_full) == os.path.abspath(dst_full):
            return src_rel
            
        os.makedirs(os.path.dirname(dst_full), exist_ok=True)
        if os.path.exists(dst_full):
            os.remove(dst_full) # 覆盖
            
        shutil.move(src_full, dst_full)
        
        # 移动缩略图
        src_thumb = self._path_join(THUMBNAIL_FOLDER, self._thumbnail_rel_path(src_rel))
        dst_thumb = self._path_join(THUMBNAIL_FOLDER, self._thumbnail_rel_path(dst_rel))
        if os.path.exists(src_thumb):
            os.makedirs(os.path.dirname(dst_thumb), exist_ok=True)
            if os.path.exists(dst_thumb): os.remove(dst_thumb)
            shutil.move(src_thumb, dst_thumb)
            
        return dst_rel

    def get_common_tags(self, limit=100, offset=0, query=""):
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        # 统计每个 tag 的引用次数
        # 注意：这里统计的是 tag.id，后面需要聚合 synonym
        
        sql = """
            SELECT t.name, COUNT(it.image_md5) as cnt 
            FROM tags t 
            JOIN image_tags it ON t.id = it.tag_id 
            GROUP BY t.id 
        """
        cursor.execute(sql)
        raw_stats = cursor.fetchall() # [(name, count), ...]
        conn.close()
        
        # Python 层面聚合 (处理同义词合并)
        merged_stats = {}
        for row in raw_stats:
            raw_tag, count = row['name'], row['cnt']
            root = self.synonym_leaf_to_root.get(raw_tag, raw_tag)
            
            if root not in merged_stats:
                merged_stats[root] = {"tag": root, "count": 0, "synonyms": self.synonym_map.get(root, [])}
            merged_stats[root]["count"] += count
            
        # 过滤与排序
        result_list = []
        q_lower = query.lower().strip()
        
        for root, data in merged_stats.items():
            if q_lower:
                match_main = q_lower in root.lower()
                match_syn = any(q_lower in s.lower() for s in data['synonyms'])
                if not (match_main or match_syn): continue
            result_list.append(data)
            
        result_list.sort(key=lambda x: x['count'], reverse=True)
        total = len(result_list)
        return {"tags": result_list[offset:offset+limit], "total": total}

    def _build_search_query(self, include_tags, exclude_tags, min_tags=None, max_tags=None):
        """构建 SQL 搜索语句，支持包含、排除、数量筛选"""
        # 核心思想：利用 INTERSECT 和 EXCEPT
        
        # 1. Include: MD5 must be in (Images with Tag A) AND (Images with Tag B) ...
        include_sqls = []
        params = []
        
        for tag in include_tags:
            variants = self.get_all_variants(tag)
            placeholders = ','.join(['?'] * len(variants))
            # 查找拥有这些标签任意一个的图片 MD5
            sql = f"""
                SELECT image_md5 FROM image_tags 
                JOIN tags ON image_tags.tag_id = tags.id 
                WHERE tags.name IN ({placeholders})
            """
            include_sqls.append(sql)
            params.extend(variants)
            
        # 2. Exclude: MD5 must NOT be in (Images with Tag C) ...
        exclude_sql = ""
        if exclude_tags:
            all_exclude_variants = []
            for tag in exclude_tags:
                all_exclude_variants.extend(self.get_all_variants(tag))
            
            if all_exclude_variants:
                placeholders = ','.join(['?'] * len(all_exclude_variants))
                exclude_sql = f"""
                    SELECT image_md5 FROM image_tags 
                    JOIN tags ON image_tags.tag_id = tags.id 
                    WHERE tags.name IN ({placeholders})
                """
                params.extend(all_exclude_variants)

        # 3. Min/Max Tags Count
        count_filter_sql = ""
        if min_tags is not None or max_tags is not None:
            p_min = int(min_tags) if min_tags is not None else 0
            p_max = int(max_tags) if max_tags is not None else 999999
            
            count_filter_sql = f"""
                SELECT image_md5 FROM image_tags 
                GROUP BY image_md5 
                HAVING COUNT(*) >= {p_min} AND COUNT(*) <= {p_max}
            """

        # 组合最终 SQL
        # 基础集合是所有图片
        final_parts = []
        
        if not include_tags and not exclude_tags and not count_filter_sql:
            # 无任何条件 -> 所有图片
            final_sql = "SELECT md5, filename FROM images"
        else:
            # 有条件 -> 计算 MD5 集合
            if include_sqls:
                # Intersect all includes
                md5_set_sql = " INTERSECT ".join(include_sqls)
            else:
                # 如果没有 include，初始集合是所有图片
                md5_set_sql = "SELECT md5 FROM images"

            if exclude_sql:
                md5_set_sql = f"({md5_set_sql}) EXCEPT ({exclude_sql})"
                
            if count_filter_sql:
                # 再次取交集 (符合数量限制的)
                md5_set_sql = f"({md5_set_sql}) INTERSECT ({count_filter_sql})"

            final_sql = f"SELECT md5, filename FROM images WHERE md5 IN ({md5_set_sql})"

        # 自动过滤 Trash (除非显式包含)
        has_trash_in_include = any(TRASH_TAG in self.get_all_variants(t) for t in include_tags)
        if not has_trash_in_include:
            trash_filter = f"""
                EXCEPT 
                SELECT image_md5 FROM image_tags 
                JOIN tags ON image_tags.tag_id = tags.id 
                WHERE tags.name = '{TRASH_TAG}'
            """
            # 如果 final_sql 已经是复杂查询，我们在最后套一层
            final_sql = f"SELECT md5, filename FROM images WHERE md5 IN (SELECT md5 FROM ({final_sql}) {trash_filter})"

        return final_sql, params

    def search(self, include, exclude, offset, limit):
        sql, params = self._build_search_query(include, exclude)
        
        # 添加分页和排序
        full_sql = f"{sql} ORDER BY filename ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        # 获取总数 (有点慢，但这在 SQLite 也是标准做法)
        count_sql = f"SELECT COUNT(*) as cnt FROM ({sql})"
        cursor.execute(count_sql, params[:-2])
        total = cursor.fetchone()['cnt']
        
        # 获取数据
        cursor.execute(full_sql, params)
        rows = cursor.fetchall()
        
        results = []
        for r in rows:
            # 性能注意：这里 N+1 获取 tags，为了前端展示。
            # 如果太慢，可以考虑 group_concat
            tags = self._get_tags_for_image(conn, r['md5'])
            results.append(self._build_image_payload(r['filename'], tags, r['md5']))
            
        conn.close()
        return {"results": results, "total": total}

    def semantic_search(self, query_text, offset, limit):
        if not query_text: return {"results": [], "total": 0}
        
        # 1. 使用 Jieba 分词
        tokens = list(jieba.cut_for_search(query_text))
        tokens = [t.strip() for t in tokens if t.strip()]
        
        if not tokens: return {"results": [], "total": 0}
        
        # 2. 扩展同义词
        # 逻辑：任一 token 命中图片标签（或同义词），即视为匹配
        # 使用 LIKE 进行模糊匹配，或者精确匹配扩展后的词
        
        # 方案：构建 OR 查询
        # SELECT DISTINCT i.* FROM images i JOIN image_tags it... JOIN tags t...
        # WHERE t.name LIKE '%token1%' OR t.name LIKE '%token2%' ...
        
        where_clauses = []
        params = []
        
        for token in tokens:
            # 扩展同义词
            variants = self.get_all_variants(token)
            # 对每个变体做模糊匹配 (LIKE %word%)
            for v in variants:
                where_clauses.append("t.name LIKE ?")
                params.append(f"%{v}%")
                
        where_str = " OR ".join(where_clauses)
        
        sql = f"""
            SELECT DISTINCT i.md5, i.filename 
            FROM images i
            JOIN image_tags it ON i.md5 = it.image_md5
            JOIN tags t ON it.tag_id = t.id
            WHERE {where_str}
        """
        
        # 过滤 trash
        sql = f"""
            SELECT * FROM ({sql}) 
            WHERE md5 NOT IN (
                SELECT image_md5 FROM image_tags 
                JOIN tags ON image_tags.tag_id = tags.id 
                WHERE tags.name = '{TRASH_TAG}'
            )
        """
        
        # 分页
        full_sql = f"{sql} ORDER BY i.filename LIMIT ? OFFSET ?"
        count_sql = f"SELECT COUNT(*) as cnt FROM ({sql})"
        
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        cursor.execute(count_sql, params)
        total = cursor.fetchone()['cnt']
        
        cursor.execute(full_sql, params + [limit, offset])
        rows = cursor.fetchall()
        
        results = []
        for r in rows:
            tags = self._get_tags_for_image(conn, r['md5'])
            results.append(self._build_image_payload(r['filename'], tags, r['md5']))
            
        conn.close()
        return {"results": results, "total": total}

    def browse(self, filter_type, tags, offset, limit, min_tags=None, max_tags=None):
        # 复用 search 逻辑，但加入 filter_type 处理
        # browse 接口通常用于特定 tag 点击或筛选
        
        # 如果是 filter_type='untagged'，可以复用 get_next_untagged 的逻辑思路，
        # 但这里需要分页。
        
        include = tags if tags else []
        exclude = []
        
        # 特殊处理 untagged
        if filter_type == 'untagged':
            # 在 search 中很难直接表达 "untagged" 同时又 include tags (矛盾)
            # 通常 untagged 不会带 include tags
            sql = f"SELECT md5, filename FROM images WHERE md5 NOT IN (SELECT image_md5 FROM image_tags)"
            # 过滤 trash
            sql += f" AND md5 NOT IN (SELECT image_md5 FROM image_tags JOIN tags ON image_tags.tag_id=tags.id WHERE tags.name='{TRASH_TAG}')"
            params = []
        else:
            # filter_type == 'all' or 'tagged'
            # 如果是 tagged，我们加上 min_tags=1 的隐含条件 (如果用户没指定 min)
            if filter_type == 'tagged' and min_tags is None:
                min_tags = 1
                
            sql, params = self._build_search_query(include, exclude, min_tags, max_tags)
            
        # 分页执行
        full_sql = f"{sql} ORDER BY filename ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        cursor.execute(f"SELECT COUNT(*) as cnt FROM ({sql})", params[:-2])
        total = cursor.fetchone()['cnt']
        
        cursor.execute(full_sql, params)
        rows = cursor.fetchall()
        
        results = []
        for r in rows:
            tags = self._get_tags_for_image(conn, r['md5'])
            results.append(self._build_image_payload(r['filename'], tags, r['md5']))
            
        conn.close()
        return {"results": results, "total": total}

    # --- 导入/导出/检查 ---

    def check_md5_exists(self, md5_val: str):
        if not md5_val: return {"exists": False, "message": "No MD5"}
        conn = self.db.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM images WHERE md5 = ?", (md5_val,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            # 还要获取 tags
            conn = self.db.get_conn()
            tags = self._get_tags_for_image(conn, md5_val)
            conn.close()
            payload = self._build_image_payload(row['filename'], tags, md5_val)
            payload["exists"] = True
            payload["message"] = "Exists"
            return payload
        return {"exists": False, "md5": md5_val}

    def check_upload(self, file_obj, provided_md5=None):
        md5_calc = calculate_md5(file_stream=file_obj)
        file_obj.seek(0)
        
        if provided_md5 and provided_md5.lower() != md5_calc:
            return {"exists": False, "error": True, "message": "MD5 mismatch"}
            
        check = self.check_md5_exists(md5_calc)
        if check.get("exists"):
            return check
            
        # 保存文件
        ext = os.path.splitext(file_obj.filename)[1].lower()
        if ext not in {'.'+e for e in ALLOWED_EXTS}: ext = '.jpg'
        new_fname = f"{md5_calc}{ext}"
        save_path = os.path.join(IMAGE_FOLDER, new_fname)
        file_obj.save(save_path)
        self._ensure_thumbnail_exists(new_fname)
        
        # 写入 DB
        conn = self.db.get_conn()
        try:
            conn.execute("INSERT INTO images (md5, filename) VALUES (?, ?)", (md5_calc, new_fname))
            conn.commit()
        finally:
            conn.close()
            
        return self._build_image_payload(new_fname, [], md5_calc)

    def import_json(self, data):
        if self.task_lock.locked():
             yield json.dumps({"status": "waiting", "message": "Waiting for lock..."}) + "\n"
             
        with self.task_lock:
            try:
                # 1. Synonyms
                if "tag_synonyms" in data:
                    self._save_synonyms(data["tag_synonyms"])
                    
                # 2. Images
                if "images" in data:
                    conn = self.db.get_conn()
                    cursor = conn.cursor()
                    images = data["images"]
                    count = 0
                    
                    # 为了性能，使用事务
                    cursor.execute("BEGIN TRANSACTION")
                    for fname, info in images.items():
                        md5 = info.get("md5")
                        tags = info.get("tags", [])
                        if not md5: continue
                        
                        # Update Image (ensure exists)
                        cursor.execute("INSERT OR IGNORE INTO images (md5, filename) VALUES (?, ?)", (md5, fname))
                        # 如果已存在，更新 filename
                        cursor.execute("UPDATE images SET filename = ? WHERE md5 = ?", (fname, md5))
                        
                        # Update Tags
                        # 先清空旧 tags
                        cursor.execute("DELETE FROM image_tags WHERE image_md5 = ?", (md5,))
                        
                        # 插入新 tags
                        for t in tags:
                            t = str(t).strip()
                            if not t: continue
                            cursor.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                            cursor.execute("SELECT id FROM tags WHERE name = ?", (t,))
                            tid = cursor.fetchone()['id']
                            cursor.execute("INSERT OR IGNORE INTO image_tags (image_md5, tag_id) VALUES (?, ?)", (md5, tid))
                            
                        count += 1
                        if count % 100 == 0:
                            pass # batch commit handled by outer transaction
                            
                    cursor.execute("COMMIT")
                    conn.close()
                    yield json.dumps({"status": "success", "message": f"Imported {count} items"}) + "\n"
                    
            except Exception as e:
                traceback.print_exc()
                yield json.dumps({"status": "error", "message": str(e)}) + "\n"

    def export_json(self):
        conn = self.db.get_conn()
        cursor = conn.cursor()
        
        export_data = {
            "tag_synonyms": self.synonym_map,
            "images": {}
        }
        
        # 获取所有图片和标签 (Group Concat 优化查询次数)
        cursor.execute("""
            SELECT i.filename, i.md5, GROUP_CONCAT(t.name) as tags_str
            FROM images i
            LEFT JOIN image_tags it ON i.md5 = it.image_md5
            LEFT JOIN tags t ON it.tag_id = t.id
            GROUP BY i.md5
        """)
        
        for row in cursor.fetchall():
            tags = row['tags_str'].split(',') if row['tags_str'] else []
            export_data["images"][row['filename']] = {
                "md5": row['md5'],
                "tags": tags
            }
            
        conn.close()
        return json.dumps(export_data, ensure_ascii=False, indent=2)

    def update_tag_group(self, main_tag, synonyms):
        new_map = self.synonym_map.copy()
        new_map[main_tag] = synonyms
        # 清理反向冲突
        for s in synonyms:
            if s in new_map and s != main_tag:
                del new_map[s]
        self._save_synonyms(new_map)
        return True

# =========================
# 4. Flask App
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
@app.route('/favicon.ico')
def fav(): return send_file('favicon.ico')
@app.route('/images/<path:f>')
def img(f): return send_from_directory(IMAGE_FOLDER, f)
@app.route('/thumbnails/<path:f>')
def thumbnail(f): return send_from_directory(THUMBNAIL_FOLDER, f)

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
    return jsonify(dm.browse(
        request.args.get('filter', 'all'), 
        t, 
        request.args.get('offset', 0, int), 
        request.args.get('limit', 50, int),
        min_tags=request.args.get('min_tags'),
        max_tags=request.args.get('max_tags')
    ))

@app.route('/api/get_common_tags')
def common():
    return jsonify(dm.get_common_tags(request.args.get('limit', 100, int), request.args.get('offset', 0, int), request.args.get('query', '')))

@app.route('/api/import_json', methods=['POST'])
def import_data():
    if 'file' not in request.files: return jsonify({"success": False}), 400
    try:
        data = json.loads(request.files['file'].read())
        return Response(stream_with_context(dm.import_json(data)), mimetype='application/json')
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400

@app.route('/api/export_json')
def export(): 
    return Response(dm.export_json(), mimetype='application/json', headers={'Content-Disposition': 'attachment;filename=meme_db_sqlite_backup.json'})

@app.route('/api/check_md5_exists')
def chk_md5(): return jsonify(dm.check_md5_exists(request.args.get('md5', '').strip()))

@app.route('/api/check_upload', methods=['POST'])
def chk_up(): 
    if 'file' not in request.files: return jsonify({"success": False}), 400
    return jsonify(dm.check_upload(request.files['file'], request.form.get('md5')))

@app.route('/api/update_synonyms', methods=['POST'])
def up_syn(): return jsonify({"success": dm.update_tag_group(request.json.get('main_tag'), request.json.get('synonyms', []))})

@app.route('/api/manual_scan')
def manual_scan():
    if request.args.get('key') != 'bqbq_secure_scan_key_2025_v1': return "Invalid Key", 403
    threading.Thread(target=dm._sync_files_to_db).start()
    return "Scan started in background"

@app.route('/api/get_image_info')
def get_info():
    md5 = request.args.get('md5')
    fname = request.args.get('filename')
    conn = dm.db.get_conn()
    cursor = conn.cursor()
    if md5: cursor.execute("SELECT * FROM images WHERE md5=?", (md5,))
    elif fname: cursor.execute("SELECT * FROM images WHERE filename=?", (fname,))
    else: return jsonify({"success": False}), 400
    
    row = cursor.fetchone()
    if row:
        tags = dm._get_tags_for_image(conn, row['md5'])
        conn.close()
        return jsonify({"success": True, **dm._build_image_payload(row['filename'], tags, row['md5'])})
    conn.close()
    return jsonify({"success": False}), 404

@app.route('/api/get_by_offset')
def get_offset():
    offset = request.args.get('offset', 0, int)
    mode = request.args.get('mode', 'json')
    conn = dm.db.get_conn()
    cursor = conn.cursor()
    
    # 获取总数
    cursor.execute("SELECT COUNT(*) as cnt FROM images")
    total = cursor.fetchone()['cnt']
    
    # 获取指定 offset 的图
    # 必须保证排序一致性
    cursor.execute("SELECT * FROM images ORDER BY filename ASC LIMIT 1 OFFSET ?", (offset,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return "Not found", 404
        
    tags = dm._get_tags_for_image(conn, row['md5'])
    conn.close()
    payload = dm._build_image_payload(row['filename'], tags, row['md5'])
    
    if mode == 'image': return redirect(payload['url'])
    return jsonify({"success": True, "total": total, "offset": offset, **payload})

if __name__ == '__main__':
    app.run(port=5000, debug=True)