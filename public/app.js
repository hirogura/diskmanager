(function(){
    const PAGES = [
        ['partition','パーティション操作','ディスク構成の確認と、パーティションの作成・削除・サイズ変更。'],
        ['smart','S.M.A.R.T.情報','各ディスクの S.M.A.R.T. 属性と健全性を表示します。'],
        ['clone','Clonezilla','Clonezilla を使った高速ディスククローンを実行します。'],
        ['rescue','ddrescue','ddrescue によるレスキュー（ディスク・イメージコピー）を実行します。'],
        ['rsync','rsync','フォルダ・ファイル単位のミラーリングコピーを行います。'],
        ['wipe','ディスク完全消去','ディスク全体をゼロ／乱数で上書き消去します。'],
        ['update','アップデート','GitHub (hirogura/diskmanager) から最新版へ更新します。'],
        ['restart','再起動','Disk Manager サービスを再起動します。'],
        ['refresh','リフレッシュ','表示中の機能を再読み込みします。']
    ];
    const PAGE_MAP = new Map(PAGES.map(([key, title, description]) => [key, { title, description }]));
    const SYSTEM_PAGES = new Set(['update','restart','refresh']);

    const panes = document.getElementById('panes');
    const systemPane = document.getElementById('system-pane');
    const pageTitle = document.getElementById('page-title');
    const pageDescription = document.getElementById('page-description');
    const connection = document.getElementById('connection');
    const actionTitle = document.getElementById('action-title');
    const actionDescription = document.getElementById('action-description');
    const actionButton = document.getElementById('action-button');
    const actionStatus = document.getElementById('action-status');
    const reloadButton = document.getElementById('reload-button');
    const updateLog = document.getElementById('update-log');

    const iframes = new Map();
    let currentPage = null;
    let updateTimer = null;
    let updateStart = 0;

    function notify(message, type){
        const el = document.createElement('div');
        el.className = 'notify ' + (type || 'error');
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 4000);
    }

    function setStatus(message, type){
        actionStatus.textContent = message || '';
        actionStatus.className = type || '';
    }

    function resetActionButtons(){
        if (currentPage === 'update') actionButton.textContent = 'アップデートを実行';
        else if (currentPage === 'restart') actionButton.textContent = '再起動を実行';
        else if (currentPage === 'refresh') actionButton.textContent = '再読み込み';
        else actionButton.textContent = '実行';
        actionButton.disabled = false;
    }

    function getPane(page){
        if (!iframes.has(page)){
            const wrap = document.createElement('div');
            wrap.className = 'pane';
            wrap.hidden = true;
            const frame = document.createElement('iframe');
            frame.src = '/' + page + '.html';
            frame.title = PAGE_MAP.get(page).title;
            wrap.appendChild(frame);
            panes.appendChild(wrap);
            iframes.set(page, wrap);
        }
        return iframes.get(page);
    }

    function showPage(page, push){
        if (!PAGE_MAP.has(page)) page = 'partition';
        currentPage = page;
        PAGES.forEach(([name]) => { if (!SYSTEM_PAGES.has(name)) getPane(name).hidden = name !== page; });
        const feature = PAGE_MAP.get(page);
        pageTitle.textContent = feature.title;
        pageDescription.textContent = feature.description;
        document.querySelectorAll('.sidebar nav button[data-page]').forEach(btn => {
            if (btn.dataset.page === page) btn.setAttribute('aria-current','page');
            else btn.removeAttribute('aria-current');
        });
        if (SYSTEM_PAGES.has(page)){ systemPane.hidden = false; panes.style.display = 'none'; renderSystemPage(page); }
        else { systemPane.hidden = true; panes.style.display = ''; updateLog.hidden = true; setStatus(''); resetActionButtons(); }
        if (push !== false && ('#' + page) !== location.hash) history.replaceState(null, '', '#' + page);
    }

    function renderSystemPage(page){
        clearInterval(updateTimer); updateTimer = null;
        if (page === 'update'){
            actionTitle.textContent = 'アップデート';
            actionDescription.textContent = 'GitHub の hirogura/diskmanager (main) から最新版を取得し、インストーラーがサービスを再起動します。実行中のタスクがある場合は中断してから実行してください。';
            actionButton.textContent = 'アップデートを実行';
            actionButton.onclick = doUpdate;
        } else if (page === 'restart'){
            actionTitle.textContent = '再起動';
            actionDescription.textContent = 'Disk Manager のサービスを再起動します。実行中のタスクがある場合は中断してから実行してください。';
            actionButton.textContent = '再起動を実行';
            actionButton.onclick = doRestart;
        } else {
            actionTitle.textContent = 'リフレッシュ';
            actionDescription.textContent = 'すべての機能ペインとサービス情報を再読み込みします。';
            actionButton.textContent = '再読み込み';
            actionButton.onclick = () => location.reload();
        }
        setStatus('');
    }

    async function doUpdate(){
        try {
            const st = await (await fetch('/api/status')).json();
            if (st.running){ notify('レスキュー実行中はアップデートできません'); return; }
            if (!confirm('GitHub から最新版を取得してアップデートしますか？\n完了後、画面にメッセージが表示されます（数分かかる場合があります）。')) return;
            actionButton.disabled = true;
            actionButton.textContent = 'アップデート中...';
            setStatus('インストーラーを起動しました。完了を待っています...');
            const res = await fetch('/api/update', { method: 'POST' });
            const data = await res.json();
            if (data.error){ resetActionButtons(); setStatus('エラー: ' + data.error, 'error'); return; }
            waitForRestart(st.version);
        } catch(e){ resetActionButtons(); setStatus('通信エラー: ' + e.message, 'error'); }
    }

    async function doRestart(){
        try {
            const st = await (await fetch('/api/status')).json();
            if (st.running){ notify('レスキュー実行中は再起動できません'); return; }
            if (!confirm('Disk Manager を再起動しますか？')) return;
            actionButton.disabled = true;
            actionButton.textContent = '再起動中...';
            const res = await fetch('/api/restart', { method: 'POST' });
            const data = await res.json();
            if (data.error){ resetActionButtons(); setStatus('エラー: ' + data.error, 'error'); return; }
            setTimeout(() => waitForRestart(null), 3000);
        } catch(e){ resetActionButtons(); setStatus('通信エラー: ' + e.message, 'error'); }
    }

    function waitForRestart(oldVersion){
        clearInterval(updateTimer);
        updateStart = Date.now();
        updateTimer = setInterval(async () => {
            try {
                const st = await (await fetch('/api/status')).json();
                let done = false, msg = '';
                if (oldVersion === null){ done = true; msg = '再起動が完了しました'; }
                else if (st.version !== oldVersion){ done = true; msg = 'アップデートが完了しました（v.' + st.version + '）'; }
                else {
                    try {
                        const ld = await (await fetch('/api/log-content?name=update.log&lines=15')).json();
                        if ((ld.content || '').includes('Done!')){ done = true; msg = 'すでに最新版です（v.' + st.version + '）'; }
                    } catch(e2){}
                }
                if (done){
                    clearInterval(updateTimer); updateTimer = null;
                    resetActionButtons();
                    setStatus(msg, 'ok');
                    reloadButton.hidden = false;
                    return;
                }
            } catch(e){}
            if (Date.now() - updateStart > 180000){
                clearInterval(updateTimer); updateTimer = null;
                resetActionButtons();
                setStatus('タイムアウトしました。手動で再読込してください', 'error');
                reloadButton.hidden = false;
            }
        }, 2000);
    }

    async function checkStatus(){
        try {
            const st = await (await fetch('/api/status')).json();
            connection.textContent = '接続中';
            connection.className = 'online';
            const v = 'v.' + st.version;
            document.getElementById('version').textContent = v;
            document.title = 'Disk Manager ' + v;
        } catch(e){
            connection.textContent = 'オフライン';
            connection.className = 'offline';
        }
    }

    window.addEventListener('hashchange', () => {
        showPage(location.hash.slice(1) || 'partition', false);
    });
    document.querySelectorAll('.sidebar nav button[data-page]').forEach(btn => {
        btn.addEventListener('click', () => showPage(btn.dataset.page, true));
    });
    reloadButton.addEventListener('click', () => location.reload());

    showPage(location.hash.slice(1) || 'partition', false);
    checkStatus();
    setInterval(checkStatus, 5000);
})();
