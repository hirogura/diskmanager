(function(){
    var FEATURES = { rescue:1, partition:1, smart:1, clone:1, rsync:1, wipe:1 };
    function inIframe(){ try { return window.self !== window.top; } catch(e){ return true; } }
    if (inIframe()) {
        document.documentElement.classList.add('in-pane');
        return;
    }
    var path = location.pathname.replace(/^\/+|\/+$/g, '');
    if (!path.endsWith('.html')) return;
    var name = path.slice(0, -5);
    if (!FEATURES[name]) return;
    location.replace('/#' + name);
}());
