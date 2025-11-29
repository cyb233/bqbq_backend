/**
 * 优化的工具函数
 */
const debounce = (func, wait) => {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
};

/**
 * 自动补全组件
 */
class TagAutocomplete {
    constructor(inputElement, submitCallback, fetchTagsCallback, mode = 'single') {
        this.inp = inputElement;
        this.submitCallback = submitCallback;
        this.fetchTagsCallback = fetchTagsCallback;
        this.mode = mode;
        this.currentFocus = -1;
        // 支持：空格, 英文逗号, 中文逗号, 中文顿号
        this.delimiters = /[ ,，、]+/;
        
        this.inp.addEventListener("input", debounce(this.handleInput.bind(this), 200));
        this.inp.addEventListener("keydown", this.handleKeydown.bind(this));
        document.addEventListener("click", (e) => { if (e.target !== this.inp) this.closeAllLists(); });
    }

    async handleInput() {
        let val = this.inp.value;
        if (this.mode !== 'single') {
            const parts = val.split(this.delimiters);
            val = parts.pop(); 
        }
        
        this.closeAllLists();
        if (!val) return;

        const data = await this.fetchTagsCallback(val);
        if (data && data.length > 0) this.renderList(data);
    }

    renderList(tags) {
        const listDiv = document.createElement("DIV");
        listDiv.className = "autocomplete-items";
        this.inp.parentNode.appendChild(listDiv);

        tags.forEach(item => {
            const div = document.createElement("DIV");
            let label = `<span class="font-bold text-gray-800">${item.tag}</span>`;
            if (item.synonyms && item.synonyms.length) {
                label += `<span class="text-xs text-gray-400 ml-2">(${item.synonyms.slice(0,2).join(', ')}${item.synonyms.length>2?'...':''})</span>`;
            }
            div.innerHTML = label;
            
            div.addEventListener("click", () => {
                if (this.mode === 'single') {
                    this.inp.value = "";
                    this.submitCallback(item.tag);
                } else {
                    // 多标签模式下，点击补全，保留之前的标签
                    const parts = this.inp.value.split(/([ ,，、]+)/);
                    // 移除最后一个正在输入的部分
                    while(parts.length && !parts[parts.length-1].trim() && !this.delimiters.test(parts[parts.length-1])) parts.pop();
                    if(parts.length && !this.delimiters.test(parts[parts.length-1])) parts.pop();
                    
                    this.inp.value = parts.join("") + item.tag + " ";
                    this.inp.focus();
                }
                this.closeAllLists();
            });
            listDiv.appendChild(div);
        });
    }

    handleKeydown(e) {
        let x = this.inp.parentNode.querySelector(".autocomplete-items");
        if (x) x = x.getElementsByTagName("div");
        
        if (e.keyCode == 40) { // Down
            this.currentFocus++; 
            this.addActive(x); 
        } else if (e.keyCode == 38) { // Up
            this.currentFocus--; 
            this.addActive(x); 
        } else if (e.keyCode == 13) { // Enter
            // 1. 如果有选中的补全项，优先执行点击补全
            if (this.currentFocus > -1 && x && x.length > 0) {
                e.preventDefault();
                x[this.currentFocus].click();
            } 
            // 2. 单标签模式：直接提交
            else if (this.mode === 'single' && this.inp.value.trim()) {
                e.preventDefault();
                this.submitCallback(this.inp.value.trim());
                this.inp.value = "";
                this.closeAllLists();
            }
            // 3. Multi 模式下：如果没有选中项，则不 preventDefault
            // 让外部的 listener 去捕获（用于执行搜索）或者让 input 自身处理
        }
    }

    addActive(x) {
        if (!x) return;
        Array.from(x).forEach(el => el.classList.remove("autocomplete-active"));
        if (this.currentFocus >= x.length) this.currentFocus = 0;
        if (this.currentFocus < 0) this.currentFocus = (x.length - 1);
        x[this.currentFocus].classList.add("autocomplete-active");
    }

    closeAllLists() {
        const x = document.getElementsByClassName("autocomplete-items");
        for (let i = 0; i < x.length; i++) x[i].remove();
        this.currentFocus = -1;
    }
}

/**
 * 同义词模态框
 */
class SynonymModalManager {
    constructor(app) {
        this.app = app;
        this.modal = document.getElementById('synonym-modal');
        this.content = document.getElementById('synonym-modal-content');
        this.currentMain = null;
        this.tagsSet = new Set(); 
        this.bindEvents();
    }

    bindEvents() {
        document.getElementById('close-modal').onclick = () => this.close();
        document.getElementById('modal-cancel-btn').onclick = () => this.close();
        
        document.getElementById('modal-add-tag-btn').onclick = () => {
            const inp = document.getElementById('modal-new-tag-input');
            const val = inp.value.trim();
            if(val) { this.tagsSet.add(val); inp.value = ''; this.render(); }
        };

        document.getElementById('modal-save-btn').onclick = async () => {
            if (!this.currentMain) return;
            const synonyms = Array.from(this.tagsSet).filter(t => t !== this.currentMain);
            await this.app.api('/api/update_synonyms', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ main_tag: this.currentMain, synonyms: synonyms })
            });
            this.app.toast(`标签组已更新: ${this.currentMain}`);
            this.app.clearCommonTagsCache();
            this.close();
            this.app.refreshCurrentViewTags();
        };
    }

    open(mainTag, synonyms = []) {
        this.currentMain = mainTag;
        this.tagsSet = new Set([mainTag, ...synonyms]);
        this.modal.classList.remove('hidden');
        this.modal.classList.add('flex');
        setTimeout(() => {
            this.content.classList.remove('scale-95', 'opacity-0');
            this.content.classList.add('scale-100', 'opacity-100');
        }, 10);
        this.render();
    }

    close() {
        this.content.classList.remove('scale-100', 'opacity-100');
        this.content.classList.add('scale-95', 'opacity-0');
        setTimeout(() => {
            this.modal.classList.add('hidden');
            this.modal.classList.remove('flex');
        }, 200);
    }

    render() {
        const display = document.getElementById('modal-main-tag-display');
        const list = document.getElementById('modal-tags-list');
        display.textContent = this.currentMain;
        list.innerHTML = '';
        const sortedTags = Array.from(this.tagsSet).sort();

        sortedTags.forEach(tag => {
            if (tag === this.currentMain) return; 
            const chip = document.createElement('div');
            chip.className = "px-3 py-1 bg-white border border-gray-200 rounded-lg text-sm flex items-center gap-2 cursor-pointer hover:border-blue-400 hover:text-blue-600 transition select-none shadow-sm font-medium";
            chip.innerHTML = `<span>${tag}</span> <span class="text-gray-300 hover:text-red-500 font-bold text-xs px-1" title="移除">&times;</span>`;
            chip.querySelector('span').onclick = (e) => { e.stopPropagation(); this.currentMain = tag; this.render(); };
            chip.querySelector('.text-gray-300').onclick = (e) => { e.stopPropagation(); this.tagsSet.delete(tag); this.render(); };
            list.appendChild(chip);
        });
        if (this.tagsSet.size <= 1) list.innerHTML = '<span class="text-xs text-gray-400 italic p-2">暂无同义词</span>';
    }
}

/**
 * 主应用逻辑
 */
class MemeApp {
    constructor() {
        this.state = {
            view: 'search',
            search: { offset: 0, limit: 40 },
            browse: { filter: 'all', tags: new Set(), offset: 0, limit: 40, tagsOffset: 0, minTags: 0, maxTags: -1 },
            tagging: { file: null, tags: new Set(), tagsOffset: 0, filter: 'untagged' },
            upload: { file: null, tags: new Set(), tagsOffset: 0 },
        };
        this.commonTagsCacheKey = 'common_tags_cache_v1';
        this.commonTagsCacheTTL = 5 * 60 * 1000; // 5 minutes
        this.commonTagsCache = this.restoreCommonTagsCache();
        this.taggingHistory = [];
        this.modalManager = new SynonymModalManager(this);
        this.init();
    }

    
    async api(url, opts = {}) {
        try {
            const res = await fetch(url, opts);

            // --- [核心修改] 优先拦截 413 错误 ---
            if (res.status === 413) {
                this.toast("上传失败：文件过大，超过服务器限制 (30MB)", "info");
                // 清除上传框的文件显示，防止用户以为还在上传
                const fileInput = document.getElementById('upload-file-input');
                if (fileInput) fileInput.value = '';
                return null;
            }

            // 拦截其他非 2xx 的 HTTP 错误
            if (!res.ok) {
                throw new Error(`HTTP Error: ${res.status} ${res.statusText}`);
            }

            // 只有状态码正常才解析 JSON
            return await res.json();
            
        } catch (e) {
            console.error(e); // 方便开发者调试
            this.toast("API Error: " + e.message, "error");
            return null;
        }
    }

    // --- Common Tag Cache Helpers ---
    restoreCommonTagsCache() {
        try {
            const raw = localStorage.getItem(this.commonTagsCacheKey);
            if (!raw) return {};
            const parsed = JSON.parse(raw);
            const now = Date.now();
            const fresh = {};
            Object.entries(parsed).forEach(([k, v]) => {
                if (v && typeof v.ts === 'number' && now - v.ts < this.commonTagsCacheTTL) {
                    fresh[k] = v;
                }
            });
            // 把过期的清理掉，避免缓存无限增�?
            localStorage.setItem(this.commonTagsCacheKey, JSON.stringify(fresh));
            return fresh;
        } catch (e) {
            console.warn('Restore common tag cache failed', e);
            return {};
        }
    }

    persistCommonTagsCache() {
        try {
            localStorage.setItem(this.commonTagsCacheKey, JSON.stringify(this.commonTagsCache));
        } catch (e) {
            console.warn('Persist common tag cache failed', e);
        }
    }

    trimCommonTagsCache() {
        const entries = Object.entries(this.commonTagsCache);
        const MAX = 30;
        if (entries.length <= MAX) return;
        entries.sort((a, b) => a[1].ts - b[1].ts);
        while (entries.length > MAX) {
            const [oldest] = entries.shift();
            delete this.commonTagsCache[oldest];
        }
    }

    clearCommonTagsCache() {
        this.commonTagsCache = {};
        try { localStorage.removeItem(this.commonTagsCacheKey); } catch (e) { /* ignore */ }
    }

    commonTagsCacheKeyBuilder(limit, offset, query) {
        return `${limit}|${offset}|${(query || '').trim().toLowerCase()}`;
    }

    async fetchCommonTags(limit = 60, offset = 0, query = "") {
        const key = this.commonTagsCacheKeyBuilder(limit, offset, query);
        const now = Date.now();
        const cached = this.commonTagsCache[key];
        if (cached && now - cached.ts < this.commonTagsCacheTTL) {
            return cached.data;
        }

        const res = await this.api(`/api/get_common_tags?limit=${limit}&offset=${offset}&query=${encodeURIComponent(query)}`);
        if (res && res.tags) {
            this.commonTagsCache[key] = { ts: now, data: res };
            this.trimCommonTagsCache();
            this.persistCommonTagsCache();
        }
        return res;
    }


    
    bindTagCountSlider() {
        const sliderEl = document.getElementById('tag-slider');
        const inputMin = document.getElementById('input-min-tags');
        const inputMax = document.getElementById('input-max-tags');
        const display = document.getElementById('tag-count-display');

        // 1. 折叠逻辑 (保持不变)
        const header = document.getElementById('tag-count-header');
        const content = document.getElementById('tag-count-content');
        const arrow = document.getElementById('tag-count-arrow');
        if(header && content && arrow) {
            header.onclick = () => {
                const isHidden = content.classList.contains('hidden');
                if(isHidden) {
                    content.classList.remove('hidden');
                    arrow.style.transform = 'rotate(90deg)';
                } else {
                    content.classList.add('hidden');
                    arrow.style.transform = 'rotate(0deg)';
                }
            };
        }

        // 2. 核心设置：视觉范围设为 0 - 5
        const SLIDER_MAX_VAL = 6; 
        
        noUiSlider.create(sliderEl, {
            start: [0, SLIDER_MAX_VAL], 
            connect: true,
            step: 1,
            range: {
                'min': 0,
                'max': SLIDER_MAX_VAL
            },
        });

        const debouncedLoad = debounce(() => this.loadBrowse(false), 300);

        // --- 3. 核心逻辑：滑块拖动 -> 更新输入框 ---
        sliderEl.noUiSlider.on('update', (values, handle) => {
            const v = parseInt(values[handle]); // 滑块视觉值 (0-5)
            
            if (handle === 0) { // 左手柄 (Min)
                const currentInputVal = parseInt(inputMin.value) || 0;
                // 防覆盖：只有当滑块不在最右，或输入框的值没超过上限时，才更新
                if (v < SLIDER_MAX_VAL || currentInputVal <= SLIDER_MAX_VAL) {
                    inputMin.value = v;
                    this.state.browse.minTags = v;
                }
            } else { // Right Handle (Max)
                // Case 1: 滑块没拉满 (< 5) -> 直接显示滑块数值
                if (v < SLIDER_MAX_VAL) {
                    inputMax.value = v;
                    this.state.browse.maxTags = v;
                } 
                // Case 2: 滑块拉满了 (== 5) -> 需要判断是“拖到了无限”还是“手输了大数字”
                else {
                    const currentVal = parseInt(inputMax.value);
                    
                    // 【核心修复】如果输入框里已经是 > 5 的数字 (比如 20)，就保持 20，不要变 ∞
                    if (!isNaN(currentVal) && currentVal > SLIDER_MAX_VAL) {
                         this.state.browse.maxTags = currentVal; // 确保状态也是 20
                         // 不修改 inputMax.value
                    } else {
                        // 否则（比如是从 4 拖到 5 的，或者输入框是空的），显示 ∞
                        inputMax.value = '∞';
                        this.state.browse.maxTags = -1;
                    }
                }
            }
            
            // 更新顶部文字显示
            const maxText = (this.state.browse.maxTags === -1) ? '∞' : this.state.browse.maxTags;
            const minText = this.state.browse.minTags; 
            display.textContent = `${minText} - ${maxText}`;
        });

        sliderEl.noUiSlider.on('change', () => {
            debouncedLoad();
        });

        // --- 4. 核心逻辑：输入框改变 -> 更新滑块 ---
        
        inputMin.onchange = () => {
            let val = parseInt(inputMin.value);
            if (isNaN(val) || val < 0) val = 0;
            
            // 更新真实状态 (哪怕是 100 也可以)
            this.state.browse.minTags = val;
            
            // 【关键修改】视觉限制：
            // 如果输入 10，把滑块推到 5 (SLIDER_MAX_VAL)，如果输入 3，滑块就到 3
            const visualVal = (val > SLIDER_MAX_VAL) ? SLIDER_MAX_VAL : val;
            
            // 只更新左手柄位置，保持右手柄不动 (null)
            sliderEl.noUiSlider.set([visualVal, null]); 
            
            this.loadBrowse(false);
        };

        inputMax.onchange = () => {
            let raw = inputMax.value.trim();
            let val;
            
            // 如果输入的是 ∞ 或空，视为无上限
            if (raw === '∞' || raw === '' || raw.toLowerCase() === 'inf') {
                val = -1; 
                inputMax.value = '∞';
                sliderEl.noUiSlider.set([null, SLIDER_MAX_VAL]); // 滑块推到最右
            } else {
                val = parseInt(raw);
                if (isNaN(val)) val = -1;
                
                // 1. 先更新输入框和状态 (比如 20)
                inputMax.value = val;
                this.state.browse.maxTags = val;
                
                // 2. 再设置滑块 (视觉上最多只能到 5)
                const visualVal = (val > SLIDER_MAX_VAL || val === -1) ? SLIDER_MAX_VAL : val;
                
                // 注意：这句代码执行后，会触发上面的 'update' 事件
                // 但由于我们在 update 里加了 (>5) 的判断，所以它不会反过来把 20 覆盖成 ∞
                sliderEl.noUiSlider.set([null, visualVal]);
            }
            
            this.loadBrowse(false);
        };
    }

    toast(msg, type = "success") {
        const el = document.getElementById('global-toast');
        
        let icon = '✅';
        let colorClass = 'bg-gray-800 text-white border-gray-700'; // success default

        if (type === 'error') {
            icon = '⚠️';
            colorClass = 'bg-red-50 border-red-100 text-red-600';
        } else if (type === 'info') {
            icon = '⏳';
            colorClass = 'bg-yellow-50 border-yellow-200 text-yellow-700';
        }

        el.innerHTML = `${icon} <span>${msg}</span>`;
        el.className = `fixed top-20 left-1/2 transform -translate-x-1/2 px-6 py-3 rounded-full shadow-2xl z-[100] transition-all duration-300 font-bold text-sm flex items-center gap-2 border ${colorClass}`;
        
        el.classList.remove('hidden');
        el.style.opacity = 1;
        
        // 如果是 waiting 类型的提示，显示时间稍微长一点(比如 4秒)，或者由下一条消息顶掉
        const duration = type === 'info' ? 4000 : 2500;
        
        // 清除旧的 timer 防止闪烁
        if(this.toastTimer) clearTimeout(this.toastTimer);
        
        this.toastTimer = setTimeout(() => { 
            el.style.opacity = 0; 
            setTimeout(() => el.classList.add('hidden'), 300); 
        }, duration);
    }

    init() {
        this.bindNav();
        this.bindSearch();
        this.bindBrowse();
        this.bindTagging();
        this.bindUpload();
        this.bindIO();
        
        this.bindSidebarEvents();

        this.switchView('search');
    }


    bindSidebarEvents() {
            // 查找所有侧边栏
            const sidebars = ['browse-sidebar', 'tagging-sidebar', 'upload-sidebar'];
            
            sidebars.forEach(id => {
                const el = document.getElementById(id);
                if (!el) return;

                // 找到侧边栏内部的标题头 (header)
                const header = el.querySelector('.sidebar-header');
                if (header) {
                    header.addEventListener('click', (e) => {
                        // 仅在移动端视口下生效 (桌面端 lg:h-full 已经强制展开了，JS 不干扰)
                        if (window.innerWidth >= 1024) return;

                        // 阻止事件冒泡，虽然这里是 header 点击，通常不需要，但为了保险
                        e.stopPropagation();

                        this.toggleSidebar(el);
                    });
                }
            });
    }

    toggleSidebar(element) {
        const isExpanded = element.classList.contains('h-[45dvh]');
        const arrow = element.querySelector('.sidebar-arrow');

        // 1. 手风琴效果：先折叠所有其他的侧边栏
        document.querySelectorAll('.mobile-sidebar').forEach(el => {
            if (el !== element) {
                el.classList.remove('h-[45dvh]', 'shadow-xl', 'ring-1', 'ring-blue-100');
                el.classList.add('h-[54px]');
                const a = el.querySelector('.sidebar-arrow');
                if(a) a.style.transform = 'rotate(0deg)';
            }
        });

        // 2. 切换当前侧边栏状态
        if (!isExpanded) {
            // 展开
            element.classList.remove('h-[54px]');
            element.classList.add('h-[45dvh]', 'shadow-xl', 'ring-1', 'ring-blue-100');
            if(arrow) arrow.style.transform = 'rotate(180deg)';
        } else {
            // 折叠
            element.classList.remove('h-[45dvh]', 'shadow-xl', 'ring-1', 'ring-blue-100');
            element.classList.add('h-[54px]');
            if(arrow) arrow.style.transform = 'rotate(0deg)';
        }
    }

    // ... 保持其余所有方法不变 ...




    bindNav() {
        ['search', 'browse', 'tagging', 'upload', 'io'].forEach(v => {
            document.getElementById(`nav-${v}`).onclick = () => this.switchView(v);
        });
    }

    switchView(view) {
        document.querySelectorAll('main > div').forEach(e => e.classList.add('hidden'));
        document.getElementById(`${view}-view`).classList.remove('hidden');
        
        // Reset Nav Styles
        document.querySelectorAll('nav button').forEach(b => b.className = "nav-btn");
        document.getElementById(`nav-${view}`).className = "nav-btn-active";
        
        this.state.view = view;
        this.refreshCurrentViewTags();
        
        if (view === 'browse' && !document.getElementById('browse-grid').hasChildNodes()) this.loadBrowse(false);
        if (view === 'tagging' && !this.state.tagging.file) this.loadTaggingImage();
    }

    refreshCurrentViewTags() {
        const v = this.state.view;
        if (v === 'browse') this.loadCommonTags('browse-tags-container', 'browse');
        else if (v === 'tagging') this.loadCommonTags('common-tags-container', 'tagging');
        else if (v === 'upload') this.loadCommonTags('upload-common-tags-container', 'upload');
    }

    // --- 新版卡片样式: 上图下标签，展示所有标签 ---
    createCard(item, context = 'browse') {
            // context 参数用于区分是在浏览列表('browse')、打标工作台('tagging') 还是上传('upload')
            // 从而决定删除后的回调行为
            
            const div = document.createElement('div');
            // 使用 h-full 让卡片在 Grid 中自动撑满，w-full 适配容器
            div.className = "group bg-white rounded-xl overflow-hidden border border-gray-100 hover:shadow-xl hover:shadow-gray-200 transition duration-300 flex flex-col w-full relative";
            
            const md5Display = item.md5 ? item.md5 : '???'; // 如果后端传回空字符串，显示 ???

            // [修改开始]：针对浏览模式启用沉浸式叠加布局
            if (context === 'browse') {
                div.innerHTML = `
                    <div class="relative bg-gray-100 cursor-pointer overflow-hidden group-inner h-64 w-full flex items-center justify-center">
                        <div class="absolute inset-0 opacity-5" style="background-image: radial-gradient(#000 1px, transparent 1px); background-size: 10px 10px;"></div>
                        
                        <img src="${item.url}" class="max-w-full max-h-full object-contain transition-transform duration-500 group-hover:scale-105 z-0" onclick="window.open('${item.url}')">
                        
                        <button class="delete-btn absolute top-2 left-2 bg-red-500/80 text-white hover:bg-red-600 p-2 rounded-lg shadow-sm transition opacity-0 group-hover:opacity-100 z-30 backdrop-blur-sm" title="删除图片">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                        </button>
                        
                        <button class="edit-btn absolute top-2 right-2 bg-blue-600/80 text-white hover:bg-blue-700 p-2 rounded-lg shadow-sm transition opacity-0 group-hover:opacity-100 z-30 backdrop-blur-sm" title="编辑标签">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"></path></svg>
                        </button>

                        <div class="absolute bottom-2 right-2 z-30 flex flex-col items-end group/info">
                            <div class="mb-2 hidden group-hover/info:block w-48 p-2.5 bg-gray-900/90 backdrop-blur text-white text-[10px] rounded-lg shadow-xl border border-gray-700 z-40 animate-[fadeIn_0.1s_ease-out]">
                                <p class="font-bold text-gray-100 mb-1.5 border-b border-gray-700 pb-1 break-all whitespace-normal leading-tight">${item.filename}</p>
                                <p class="font-mono text-gray-400 break-all whitespace-normal leading-tight">MD5: ${md5Display}</p>
                            </div>
                            <button class="bg-gray-800/60 text-white hover:bg-gray-900 p-2 rounded-lg shadow-sm backdrop-blur-sm transition opacity-0 group-hover:opacity-100 ring-1 ring-white/10">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                            </button>
                        </div>

                        <div class="absolute bottom-0 left-0 right-0 pt-10 pb-2 px-2 bg-gradient-to-t from-black/80 via-black/40 to-transparent flex items-end z-20 pointer-events-none min-h-[60px]">
                            <div class="flex flex-wrap gap-1.5 content-end pr-10 pointer-events-auto w-full max-h-[80px] overflow-hidden">
                                ${item.tags && item.tags.length ? 
                                    item.tags.map(t => `<span class="text-[10px] font-bold px-2 py-0.5 bg-white/20 text-white backdrop-blur-md rounded hover:bg-blue-500/80 transition cursor-default border border-white/10 shadow-sm select-none">${t}</span>`).join('') : 
                                    '' 
                                }
                            </div>
                        </div>
                    </div>
                `;
            } else {
                // [保留原样]：Tagging 和 Upload 模式保持清晰的上下结构，便于核对
                div.innerHTML = `
                    <div class="px-3 py-2 bg-gray-50 border-b border-gray-100 flex flex-col justify-center text-gray-500 select-all">
                        <span class="break-all whitespace-normal text-xs font-bold text-gray-700 mb-0.5" title="${item.filename}">${item.filename}</span>
                        <span class="break-all whitespace-normal text-[10px] font-mono text-gray-400" title="Full MD5: ${md5Display}">MD5: ${md5Display}</span>
                    </div>
                                    
                    <div class="relative bg-gray-50/50 cursor-pointer overflow-hidden border-b border-gray-50 group-inner h-64 flex items-center justify-center">
                        <div class="absolute inset-0 opacity-5" style="background-image: radial-gradient(#000 1px, transparent 1px); background-size: 10px 10px;"></div>
                        <img src="${item.url}" class="max-w-full max-h-full object-contain transition-transform duration-500 group-hover:scale-105" onclick="window.open('${item.url}')">
                        
                        <button class="delete-btn absolute top-2 left-2 bg-red-50/90 text-red-500 hover:bg-red-500 hover:text-white p-2 rounded-lg shadow-sm transition opacity-0 group-hover:opacity-100 z-10" title="删除图片">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                        </button>
                        
                        ${context !== 'tagging' && context !== 'upload' ? `
                        <button class="edit-btn absolute top-2 right-2 bg-white/90 text-blue-600 hover:bg-blue-600 hover:text-white p-2 rounded-lg shadow-sm transition opacity-0 group-hover:opacity-100 z-10" title="编辑标签">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"></path></svg>
                        </button>` : ''}
                    </div>
                    
                    <div class="p-3 bg-white flex-grow flex flex-col min-h-[60px]">
                        <div class="flex flex-wrap gap-1.5 content-start">
                            ${item.tags && item.tags.length ? 
                                item.tags.map(t => `<span class="text-[10px] font-bold px-2 py-1 bg-gray-100 text-gray-600 rounded hover:bg-blue-50 hover:text-blue-600 transition cursor-default">${t}</span>`).join('') : 
                                '<span class="text-[10px] text-gray-300 italic">暂无标签</span>'
                            }
                        </div>
                    </div>
                `;
            }
            
            // 绑定事件
            if (context === 'browse' || (context !== 'tagging' && context !== 'upload')) {
                const editBtn = div.querySelector('.edit-btn');
                if(editBtn) editBtn.onclick = (e) => { e.stopPropagation(); this.editImage(item); };
            }

            div.querySelector('.delete-btn').onclick = async (e) => {
                e.stopPropagation();
                if(confirm(`确认将 "${item.filename}" 移入回收站并删除记录？`)) {
                    const res = await this.api('/api/delete_image', {
                        method: 'POST', 
                        headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({filename: item.filename})
                    });
                    
                    if (res && res.success) {
                        this.toast('已删除');
                        // 根据上下文处理后续逻辑
                        if (context === 'browse') {
                            div.remove(); // 列表模式：移除卡片
                        } else if (context === 'tagging') {
                            this.loadTaggingImage(); // 打标模式：加载下一张
                        } else if (context === 'upload') {
                            this.resetUploadView(); // 上传模式：重置
                        }
                    }
                }
            };

            return div;
        }


    editImage(item) {
        // [修改] 重构整个 editImage 方法
        this.switchView('tagging');
        const s = this.state.tagging;
        
        // 使用 renderTaggingView 统一渲染逻辑，确保布局正确
        // 注意：createCard 里的 item 必须包含 md5 字段（由第一步 app.py 保证）
        this.renderTaggingView(item.filename, item.url, new Set(item.tags), item.md5);
        
        // 显示工作区，隐藏加载消息
        document.getElementById('tagging-workspace').classList.remove('hidden');
        document.getElementById('tagging-message').classList.add('hidden');
    }

    // --- Common Tags (标签库加载 - 优化布局版) ---
    async loadCommonTags(containerId, context, append = false, query = "") {
        const container = document.getElementById(containerId);
        if (!container) return;
        if (!append) { container.innerHTML = ''; this.state[context].tagsOffset = 0; }
        
        const limit = 60;
        const offset = this.state[context].tagsOffset;
        const res = await this.fetchCommonTags(limit, offset, query);
        
        if (res && res.tags) {
            const fragment = document.createDocumentFragment();
            res.tags.forEach(data => {
                const t = data.tag;
                const synonyms = data.synonyms || [];
                const isSelected = context === 'browse' && this.state.browse.tags.has(t);
                
                // 修改说明：将 pr-7 改为 pr-5 (预留空间从 28px 减小到 20px)
                // 使用 relative 布局，并预留右侧空间 (pr-5) 放置 hover 图标，避免宽度抖动
                const btn = document.createElement('div');
                btn.className = `relative group inline-flex items-center rounded-lg border transition-all cursor-pointer select-none overflow-hidden font-medium text-xs pr-1 h-[30px]
                    ${isSelected ? 'bg-blue-600 text-white border-blue-600 shadow-md ring-2 ring-blue-100' : 'bg-white text-gray-600 border-gray-200 hover:border-blue-400 hover:text-blue-600 hover:shadow-sm'}
                `;
                
                // 1. Main Tag Text (Truncate)
                const span = document.createElement('span');
                span.className = "px-3 truncate max-w-[140px] leading-none";
                span.innerText = t;
                span.onclick = () => {
                    if (context === 'browse') {
                        if (this.state.browse.tags.has(t)) this.state.browse.tags.delete(t);
                        else this.state.browse.tags.add(t);
                        this.refreshBrowseFilters();
                        this.loadBrowse(false);
                    } else {
                        this.addTagToState(context, t);
                    }
                };
                btn.appendChild(span);
                
                // 2. Action Icon (Absolute Positioned, Opacity Transition)
                const actionContainer = document.createElement('div');
                actionContainer.className = "absolute -right-1.5 top-1/2 -translate-y-1/2 mt-1.5 flex items-center justify-center";
                
                if (synonyms.length > 0) {
                    // 同义词指示点 (默认显示小点，hover 变大点/提示)
                    const dot = document.createElement('span');
                    dot.className = "w-1.5 h-1.5 rounded-full bg-orange-400 group-hover:scale-125 transition-transform";
                    dot.title = `包含同义词: ${synonyms.join(', ')}`;
                    // 让整个右侧区域可点
                    const clickArea = document.createElement('div');
                    clickArea.className = "w-5 h-5 flex items-center justify-center cursor-pointer hover:bg-gray-100 rounded-full transition";
                    clickArea.onclick = (e) => { e.stopPropagation(); this.modalManager.open(t, synonyms); };
                    clickArea.appendChild(dot);
                    actionContainer.appendChild(clickArea);
                } else {
                    // 编辑图标 (默认隐藏 opacity-0, hover 显示)
                    const editIcon = document.createElement('span');
                    editIcon.className = "text-gray-400 hover:text-blue-600 opacity-0 group-hover:opacity-100 transition-opacity text-[10px] cursor-pointer p-1";
                    editIcon.innerHTML = "✎";
                    editIcon.onclick = (e) => { e.stopPropagation(); this.modalManager.open(t, synonyms); };
                    actionContainer.appendChild(editIcon);
                }
                btn.appendChild(actionContainer);

                // 3. Delete Button (Absolute Top Right Badge)
                // 保持原样，它已经是 absolute 且不影响布局
                const smallDel = document.createElement('span');
                smallDel.className = "absolute -top-1.5 -right-1.5 w-4 h-4 bg-red-500 text-white rounded-full text-[10px] flex items-center justify-center opacity-0 group-hover:opacity-100 transition cursor-pointer hover:scale-110 z-30 shadow-sm border border-white";
                smallDel.innerHTML = "&times;";
                smallDel.onclick = async (e) => {
                     e.stopPropagation();
                     if(confirm(`确认从库中删除 "${t}"？`)) {
                        await this.api('/api/delete_common_tag', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tag:t})});
                        this.clearCommonTagsCache();
                        this.loadCommonTags(containerId, context);
                     }
                };
                
                btn.appendChild(smallDel);
                fragment.appendChild(btn);
            });
            container.appendChild(fragment);
            
            this.state[context].tagsOffset += res.tags.length;
            const moreBtn = document.getElementById(`${context === 'browse' ? 'browse-tags' : context === 'tagging' ? 'common-tags' : 'upload-tags'}-load-more`);
            if(moreBtn) moreBtn.classList.toggle('hidden', res.tags.length < limit);
        }
    }

    addTagToState(ctx, tag) {
        this.state[ctx].tags.add(tag);
        this.renderTagList(ctx === 'tagging' ? 'current-tags-list' : 'upload-current-tags-list', this.state[ctx].tags, ctx);
    }

    renderTagList(containerId, set, ctx) {
        const c = document.getElementById(containerId);
        const ph = document.getElementById(ctx === 'tagging' ? 'current-tags-placeholder' : '');
        if(ph) ph.style.display = set.size ? 'none' : 'inline';
        c.innerHTML = '';
        set.forEach(t => {
            const el = document.createElement('span');
            el.className = "inline-flex items-center bg-blue-50 text-blue-700 border border-blue-100 text-sm px-3 py-1.5 rounded-lg font-bold animate-[fadeIn_0.2s_ease-out]";
            el.innerHTML = `<span>${t}</span><button class="ml-2 hover:text-red-500 text-blue-300 font-bold focus:outline-none">&times;</button>`;
            el.querySelector('button').onclick = () => { set.delete(t); this.renderTagList(containerId, set, ctx); };
            c.appendChild(el);
        });
    }

    // --- Search Logic ---
    bindSearch() {
            const fetcher = async (q) => (await this.fetchCommonTags(8, 0, q))?.tags || [];
            // Autocomplete binding
            const incInput = document.getElementById('search-include');
            const excInput = document.getElementById('search-exclude');
            
            // 1. 绑定自动补全 (Multi 模式)
            new TagAutocomplete(incInput, () => {}, fetcher, 'multi');
            new TagAutocomplete(excInput, () => {}, fetcher, 'multi');


            // [新增] 搜索模式切换逻辑 & 本地存储记忆
            const toggle = document.getElementById('search-mode-toggle');
            
            // --- 步骤 A: 初始化时读取本地存储 ---
            // 检查是否有保存的设置，如果有，覆盖默认状态
            const savedSearchMode = localStorage.getItem('search_mode_semantic');
            if (savedSearchMode !== null) {
                toggle.checked = (savedSearchMode === 'true');
            }

            const titleEl = document.getElementById('search-view-title');
            const hintEl = document.getElementById('search-hint-text');
            const labelEl = document.getElementById('search-input-label');
            const excludeWrapper = document.getElementById('search-exclude-wrapper');

            const updateSearchUI = () => {
                const isSemantic = toggle.checked;
                if (isSemantic) {
                    titleEl.innerHTML = '🧠 语义搜索';
                    hintEl.textContent = '提示：输入自然语言描述，AI 将尝试理解图片的语义。';
                    labelEl.textContent = '描述内容';
                    incInput.placeholder = "例如: 看起来很困的猫...";
                    // 语义搜索通常不需要排除项，或者后端处理较复杂，这里可以视觉上弱化或隐藏，这里暂时保留但置灰
                    excludeWrapper.style.opacity = '0.5'; 
                    excludeWrapper.style.pointerEvents = 'none';
                } else {
                    titleEl.innerHTML = '🔍 精确搜索';
                    hintEl.textContent = '提示：包含标签为“且 (AND)”关系，必须同时满足。';
                    labelEl.textContent = '包含标签 (Must Include)';
                    incInput.placeholder = "例如: 熊猫头, 震惊...";
                    excludeWrapper.style.opacity = '1';
                    excludeWrapper.style.pointerEvents = 'auto';
                }
            };

            // 初始化 UI 状态

            // --- 步骤 B: 切换时写入本地存储 ---
            toggle.addEventListener('change', () => {
                // 保存当前状态 (true/false 转为字符串存储)
                localStorage.setItem('search_mode_semantic', toggle.checked);
                updateSearchUI();
            });
            
            updateSearchUI(); // 初始执行一次，确保 UI 与读取到的状态一致

            const doSearch = async (append) => {
                const s = this.state.search;
                const grid = document.getElementById('results-grid');
                const loadMore = document.getElementById('search-load-more');
                const countEl = document.getElementById('results-count');
                if (!append) { s.offset = 0; grid.innerHTML = ''; }
                
                // 支持: 空格, 英文逗号, 中文逗号, 中文顿号
                const splitRegex = /[ ,，、]+/;
                
                // 如果输入框中只有分隔符，clean up
                if(incInput.value.trim() && !incInput.value.replace(splitRegex, '')) incInput.value = '';
                if(excInput.value.trim() && !excInput.value.replace(splitRegex, '')) excInput.value = '';

                const btn = document.getElementById('search-button');
                const originalText = btn.innerHTML;
                btn.innerHTML = `<span class="animate-spin">⏳</span> 搜索中...`;
                
                // [修改] 根据开关状态选择 API 接口
                const isSemantic = toggle.checked;
                let url = '';
                
                if (isSemantic) {
                    // 语义搜索：直接传原始字符串，后端去切分或 embedding
                    const queryRaw = incInput.value.trim();
                    url = `/api/semantic_search?q=${encodeURIComponent(queryRaw)}&offset=${s.offset}&limit=${s.limit}`;
                } else {
                    // 精确搜索：前端切分标签
                    const i = incInput.value.split(splitRegex).filter(x=>x.trim()).join(',');
                    const e = excInput.value.split(splitRegex).filter(x=>x.trim()).join(',');
                    url = `/api/search?include=${encodeURIComponent(i)}&exclude=${encodeURIComponent(e)}&offset=${s.offset}&limit=${s.limit}`;
                }
                
                const res = await this.api(url);
                btn.innerHTML = originalText;
                
                if (res) {
                    countEl.textContent = `${res.total} 结果`;
                    res.results.forEach(r => grid.appendChild(this.createCard(r)));
                    s.offset += res.results.length;
                    loadMore.classList.toggle('hidden', s.offset >= res.total);
                }
            };
            
            document.getElementById('search-button').onclick = () => doSearch(false);
            document.getElementById('search-load-more').onclick = () => doSearch(true);

            // 2. 绑定回车键搜索逻辑
            const handleEnterSearch = (e) => {
                if (e.key === 'Enter' && !e.defaultPrevented) {
                    const list = e.target.parentNode.querySelector(".autocomplete-items");
                    const active = list ? list.querySelector(".autocomplete-active") : null;
                    // 如果没有选中自动补全项，则执行搜索
                    if (!active) {
                        e.preventDefault(); 
                        doSearch(false);
                    }
                }
            };

            incInput.addEventListener('keydown', handleEnterSearch);
            excInput.addEventListener('keydown', handleEnterSearch);
        }


    // --- Browse Logic ---
    bindBrowse() {

        this.bindTagCountSlider();
        // Filters
        document.querySelectorAll('.filter-chip').forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll('.filter-chip').forEach(b => b.className = 'filter-chip');
                btn.className = 'filter-chip active';
                this.state.browse.filter = btn.dataset.filter;
                this.loadBrowse(false);
            };
        });

        document.getElementById('browse-load-more').onclick = () => this.loadBrowse(true);
        document.getElementById('browse-clear-filters').onclick = () => {
            this.state.browse.tags.clear();
            this.refreshBrowseFilters();
            this.loadBrowse(false);
        };
        
        const fetcher = async (q) => (await this.fetchCommonTags(8, 0, q))?.tags || [];
        
        // 1. 标签库搜索 -> 过滤
        new TagAutocomplete(document.getElementById('browse-tag-search'), (tag) => {
            this.state.browse.tags.add(tag);
            this.refreshBrowseFilters();
            this.loadBrowse(false);
            document.getElementById('browse-tag-search').value = '';
        }, fetcher);

        // 2. 添加新标签到库
        new TagAutocomplete(document.getElementById('browse-new-tag-input'), (tag) => {
            document.getElementById('browse-new-tag-input').value = tag;
        }, fetcher);

        document.getElementById('browse-add-tag-btn').onclick = async () => {
            const inp = document.getElementById('browse-new-tag-input');
            const val = inp.value.trim();
            if(!val) return;
            await this.api('/api/add_common_tag', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tag:val})});
            this.clearCommonTagsCache();
            inp.value='';
            this.loadCommonTags('browse-tags-container', 'browse');
        };
    }
    
    refreshBrowseFilters() {
        const hasTags = this.state.browse.tags.size > 0;
        document.getElementById('browse-clear-filters').classList.toggle('hidden', !hasTags);
        this.loadCommonTags('browse-tags-container', 'browse');
        const title = hasTags ? `筛选: ${Array.from(this.state.browse.tags).join(' + ')}` : '全部图片';
        // Safely update text node only
        const titleEl = document.getElementById('browse-header-title');
        const textNode = Array.from(titleEl.childNodes).find(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
        if(textNode) textNode.textContent = ` ${title}`;
        else titleEl.innerHTML += ` ${title}`; // fallback
    }

    
    
    async loadBrowse(append) {
        const s = this.state.browse;
        const grid = document.getElementById('browse-grid');
        if (!append) { s.offset = 0; grid.innerHTML = ''; }
        
        const t = Array.from(s.tags).join(',');
        
        // Build URL
        let url = `/api/browse?filter=${s.filter}&offset=${s.offset}&limit=${s.limit}&tag=${encodeURIComponent(t)}`;
        url += `&min_tags=${s.minTags}&max_tags=${s.maxTags}`;

        const res = await this.api(url);
        
        if (res) {
            res.results.forEach(r => grid.appendChild(this.createCard(r)));
            s.offset += res.results.length;

            // --- FIX START: Update Counter Display ---
            const counter = document.getElementById('browse-counter');
            if (counter) {
                // Format: "Current Loaded / Total"
                counter.textContent = ` ${grid.childElementCount} / ${res.total}`;
                // Or if you prefer just the total: 
                // counter.textContent = ` (${res.total})`; 
            }
            // --- FIX END ---

            document.getElementById('browse-load-more').classList.toggle('hidden', s.offset >= res.total);
            document.getElementById('browse-empty-msg').classList.toggle('hidden', res.total > 0);
        }
    }

    // --- Tagging Logic ---
    bindTagging() {
        const fetcher = async (q) => (await this.fetchCommonTags(8, 0, q))?.tags || [];
        
        // 1. 打标输入框
        new TagAutocomplete(document.getElementById('tag-input'), (t) => this.addTagToState('tagging', t), fetcher);
        
        // 2. 库添加输入框
        new TagAutocomplete(document.getElementById('new-common-tag-input'), (t) => {
            document.getElementById('new-common-tag-input').value = t; 
        }, fetcher);

        document.getElementById('add-tag-btn').onclick = () => {
            const inp = document.getElementById('tag-input');
            if(inp.value.trim()) { this.addTagToState('tagging', inp.value.trim()); inp.value=''; }
        };

        // --- 新增：绑定打标页面的筛选按钮 ---
        document.querySelectorAll('.tagging-filter-chip').forEach(btn => {
            btn.onclick = () => {
                // 1. UI 切换
                document.querySelectorAll('.tagging-filter-chip').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                
                // 2. 更新状态
                this.state.tagging.filter = btn.dataset.filter;
                
                // 3. 重新加载图片（清空历史记录，因为过滤条件变了）
                this.taggingHistory = []; 
                this.loadTaggingImage();
            };
        });

        // 保存逻辑
        const save = async () => {
            const s = this.state.tagging;
            if (!s.file) return;
            if (!s.tags.size) return this.toast('请至少添加一个标签', 'error');
            
            const btn = document.getElementById('save-next-button');
            const originalText = btn.textContent;
            btn.textContent = "保存中...";
            await this.api('/api/save_tags', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({filename:s.file, tags:Array.from(s.tags)})});
            btn.textContent = originalText;
            
            this.toast('保存成功');
            // 保存并下一张时，将当前状态（已含标签）推入历史，方便回看
            this.pushHistory(); 
            this.loadTaggingImage();
        };
        
        document.getElementById('save-next-button').onclick = save;

        // --- 新增：上一张/下一张 逻辑 ---
        
        document.getElementById('prev-button').onclick = () => {
            if (this.taggingHistory.length === 0) {
                return this.toast('没有上一张记录了', 'error');
            }
            const prevItem = this.taggingHistory.pop(); // 取出上一张
            this.renderTaggingView(prevItem.file, prevItem.url, prevItem.tags, prevItem.md5);
        };

        document.getElementById('next-button').onclick = () => {
            this.pushHistory(); // 记录当前这张
            this.loadTaggingImage(); // 加载新图
        };
        
        // 库管理相关
        document.getElementById('add-common-tag-button').onclick = async () => {
            const inp = document.getElementById('new-common-tag-input');
            const val = inp.value.trim();
            if(!val) return;
            await this.api('/api/add_common_tag', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tag:val})});
            this.clearCommonTagsCache();
            inp.value='';
            this.loadCommonTags('common-tags-container', 'tagging');
        };
        document.getElementById('common-tags-load-more').onclick = () => this.loadCommonTags('common-tags-container', 'tagging', true);
    }

    // 辅助方法：记录当前状态到历史
    pushHistory() {
            const s = this.state.tagging;
            if (s.file) {
                const imgEl = document.querySelector('#tagging-card-container img');
                const currentUrl = imgEl ? imgEl.src : `/images/${s.file}`;

                this.taggingHistory.push({
                    file: s.file,
                    url: currentUrl,
                    tags: new Set(s.tags),
                    md5: s.md5 // <--- 【新增】保存 MD5 到历史记录
                });
            }
        }

    // 辅助方法：渲染打标视图 (复用逻辑)
    renderTaggingView(filename, url, tagsSet, md5 = null) { // 增加 md5 参数
            const ws = document.getElementById('tagging-workspace');
            const msg = document.getElementById('tagging-message');
            const container = document.getElementById('tagging-card-container');
            
            // 1. 更新状态
            this.state.tagging.file = filename;
            this.state.tagging.tags = tagsSet instanceof Set ? tagsSet : new Set(tagsSet);
            this.state.tagging.md5 = md5; // <--- 关键修改：保存 MD5 到状态

            // 2. 清空容器并插入新卡片
            container.innerHTML = '';
            
            const card = this.createCard({
                filename: filename,
                url: url,
                tags: Array.from(this.state.tagging.tags),
                md5: md5 // 传入 MD5 用于显示
            }, 'tagging');
            
            container.appendChild(card);
            
            // 3. 渲染右侧的操作区标签列表 
            // (虽然卡片底部也有显示，但右侧操作区是带删除按钮的，用于编辑)
            this.renderTagList('current-tags-list', this.state.tagging.tags, 'tagging');

            // 4. 切换视图显示
            ws.classList.remove('hidden');
            msg.classList.add('hidden');
        }


        async loadTaggingImage() {
            // 获取当前状态
            let currentFile = null;
            if (this.state.tagging.file) {
                currentFile = this.state.tagging.file;
            }
            const filterType = this.state.tagging.filter; // 获取当前筛选类型

            // 构建 URL，带上 filter 参数
            const url = currentFile 
                ? `/api/get_next_untagged_image?current=${encodeURIComponent(currentFile)}&filter=${filterType}`
                : `/api/get_next_untagged_image?filter=${filterType}`;

            const res = await this.api(url);
            
            const ws = document.getElementById('tagging-workspace');
            const msg = document.getElementById('tagging-message');

            if (res.success && res.filename) {
                const initialTags = res.tags ? new Set(res.tags) : new Set();
                this.renderTaggingView(res.filename, res.url, initialTags, res.md5);
                
                // 如果是“浏览/已打标”模式，或者是未打标模式但库空了转入review
                if (filterType !== 'untagged' || res.is_review) {
                    // 可选：可以在界面上显示当前进度，res.message 包含了类似 "10/500" 的信息
                }
            } else {
                // 彻底空了
                ws.classList.add('hidden');
                msg.classList.remove('hidden');
                let emptyText = "没有待处理图片";
                if (filterType === 'tagged') emptyText = "还没有已打标的图片";
                if (filterType === 'all') emptyText = "库为空";
                document.getElementById('message-text').textContent = emptyText;
            }
        }

    // --- Upload Logic (Updated with Layout & Autocomplete) ---
    bindUpload() {
        const fetcher = async (q) => (await this.fetchCommonTags(8, 0, q))?.tags || [];
        
        // 1. 上传打标输入框
        new TagAutocomplete(document.getElementById('upload-tag-input'), (t) => this.addTagToState('upload', t), fetcher);
        
        // 2. 上传页添加新词到库
        new TagAutocomplete(document.getElementById('upload-new-common-tag-input'), (t) => {
             document.getElementById('upload-new-common-tag-input').value = t;
        }, fetcher);

        document.getElementById('upload-add-tag-btn').onclick = () => {
            const inp = document.getElementById('upload-tag-input');
            if(inp.value.trim()) { this.addTagToState('upload', inp.value.trim()); inp.value=''; }
        };

        const fileInp = document.getElementById('upload-file-input');
        
        const handleFileSelect = async (file) => {
            if(!file) return;

            // --- [新增] 前端大小限制 (100MB = 100 * 1024 * 1024 字节) ---
            const MAX_SIZE = 30 * 1024 * 1024; 
            if (file.size > MAX_SIZE) {
                this.toast("文件大小超过限制 (100MB)", "error");
                // 清空当前选择，否则用户可能以为还在等待
                document.getElementById('upload-file-input').value = ''; 
                return;
            }
            // ---------------------------------------------------------

            const fd = new FormData(); fd.append('file', file);
            const res = await this.api('/api/check_upload', {method:'POST', body:fd});
            
            const msgEl = document.getElementById('upload-message');
            msgEl.classList.remove('hidden');
            msgEl.textContent = res.message;
            msgEl.className = `mb-6 p-4 rounded-xl text-center font-bold text-sm animate-none ${res.exists ? 'bg-yellow-50 text-yellow-700 border border-yellow-200' : 'bg-green-50 text-green-700 border border-green-200'}`;
            
            if(!res.error) {
                        this.state.upload.file = res.filename;
                        this.state.upload.tags = new Set(res.tags);
                        
                        document.getElementById('upload-workspace').classList.remove('hidden');
                        //document.getElementById('upload-area').classList.add('hidden');
                        
                        // 【核心修改】使用 createCard 渲染上传预览
                        const container = document.getElementById('upload-card-container');
                        container.innerHTML = '';
                        const card = this.createCard({
                            filename: res.filename,
                            url: res.url,
                            tags: res.tags,
                            md5: res.md5 // 确保 check_upload 返回了 md5
                        }, 'upload');
                        container.appendChild(card);

                        this.renderTagList('upload-current-tags-list', this.state.upload.tags, 'upload');
                    }
        }

        fileInp.onchange = (e) => handleFileSelect(e.target.files[0]);
        
        // 绑定重选按钮 (New)
        document.getElementById('upload-reselect-btn').onclick = () => fileInp.click();

        document.getElementById('upload-save-button').onclick = async () => {
             const s = this.state.upload;
             if(!s.tags.size) return this.toast('请至少添加一个标签', 'error');
             await this.api('/api/save_tags', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({filename:s.file, tags:Array.from(s.tags)})});
             this.toast('上传保存成功');
             // Reset UI to upload area to allow continuous upload
             this.resetUploadView();
        };
        
        document.getElementById('upload-cancel-btn').onclick = () => this.resetUploadView();

        document.getElementById('upload-add-common-tag-button').onclick = async () => {
            const inp = document.getElementById('upload-new-common-tag-input');
            const val = inp.value.trim();
            if(!val) return;
            await this.api('/api/add_common_tag', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tag:val})});
            this.clearCommonTagsCache();
            inp.value='';
            this.loadCommonTags('upload-common-tags-container', 'upload');
        };
        document.getElementById('upload-tags-load-more').onclick = () => this.loadCommonTags('upload-common-tags-container', 'upload', true);
    }

    resetUploadView() {
        document.getElementById('upload-workspace').classList.add('hidden');
        document.getElementById('upload-message').classList.add('hidden');
        //document.getElementById('upload-area').classList.remove('hidden');
        document.getElementById('upload-file-input').value = '';
        this.state.upload.file = null;
        this.state.upload.tags.clear();
    }

    // --- IO ---
    bindIO() {
        document.getElementById('export-button').onclick = () => window.location.href = '/api/export_json';
        const fin = document.getElementById('import-file-input');
        const btn = document.getElementById('import-confirm-btn');
        const statusEl = document.getElementById('import-status'); // 获取状态显示元素

        fin.onchange = (e) => {
            if(e.target.files[0]) {
                statusEl.textContent = `已选择: ${e.target.files[0].name}`;
                statusEl.className = "text-xs text-gray-500 font-bold mt-3 h-4"; // 重置颜色
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        };

        btn.onclick = async () => {
            const fd = new FormData(); 
            fd.append('file', fin.files[0]);
            
            btn.textContent = "处理中...";
            btn.disabled = true;
            btn.classList.add('opacity-50', 'cursor-not-allowed');

            try {
                const response = await fetch('/api/import_json', { method: 'POST', body: fd });
                
                // --- 核心修改：流式读取 ---
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    
                    // 处理完整的行，保留未完成的行在 buffer 中
                    buffer = lines.pop(); 

                    for (const line of lines) {
                        if (!line.trim()) continue;
                        try {
                            const res = JSON.parse(line);
                            
                            // 1. 如果是等待消息
                            if (res.status === 'waiting') {
                                this.toast(res.message, 'info'); // 弹窗提示
                                statusEl.textContent = "⏳ " + res.message; // 底部文字提示
                                statusEl.className = "text-xs text-orange-600 font-bold mt-3 h-4 animate-pulse";
                            } 
                            // 2. 如果是成功消息
                            else if (res.status === 'success') {
                                this.toast(res.message, 'success');
                                statusEl.textContent = "✅ " + res.message;
                                statusEl.className = "text-xs text-green-600 font-bold mt-3 h-4";
                            } 
                            // 3. 如果是错误
                            else if (res.status === 'error') {
                                this.toast(res.message, 'error');
                                statusEl.textContent = "❌ " + res.message;
                                statusEl.className = "text-xs text-red-600 font-bold mt-3 h-4";
                            }
                        } catch (e) {
                            console.error("Parse error", e);
                        }
                    }
                }
            } catch (e) {
                this.toast("请求失败: " + e.message, "error");
            } finally {
                btn.textContent = "确认覆盖导入";
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        };
    }



}

document.addEventListener('DOMContentLoaded', () => {
    // 确保整个 DOM 结构（包括 Modal 的按钮）被解析完成后才执行初始化
    window.app = new MemeApp();
});
