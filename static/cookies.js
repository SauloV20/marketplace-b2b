// Aviso de cookies (LGPD) e Google Analytics: o gtag só carrega depois que o visitante aceita.
(function () {
  var aviso = document.getElementById('aviso-cookies');
  if (!aviso) return;
  var id = aviso.getAttribute('data-ga');
  var NOME = 'cookies_ok';

  function escolha() {
    var m = document.cookie.match(/(?:^|; )cookies_ok=(sim|nao)/);
    return m ? m[1] : '';
  }
  function guardar(valor) {
    var seguro = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = NOME + '=' + valor + '; Max-Age=' + 180 * 24 * 3600 + '; Path=/; SameSite=Lax' + seguro;
  }
  function apagarCookiesDoGoogle() {
    var partes = location.hostname.split('.');
    document.cookie.split('; ').forEach(function (c) {
      var nome = c.split('=')[0];
      if (nome !== '_ga' && nome.indexOf('_ga_') !== 0) return;
      for (var i = 0; i < partes.length - 1; i++) {  // o Google grava no domínio principal
        var dominio = partes.slice(i).join('.');
        document.cookie = nome + '=; Max-Age=0; Path=/; Domain=.' + dominio;
      }
      document.cookie = nome + '=; Max-Age=0; Path=/';
    });
  }
  function carregar() {
    if (window.gtag) return;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag('js', new Date());
    window.gtag('config', id);
    var compra = aviso.getAttribute('data-ga-compra');
    if (compra) {
      try { window.gtag('event', 'purchase', JSON.parse(compra)); } catch (e) { /* dados estranhos: ignora */ }
    }
    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(id);
    document.head.appendChild(s);
  }

  aviso.addEventListener('click', function (e) {
    var botao = e.target.closest('[data-cookies]');
    if (!botao) return;
    var valor = botao.getAttribute('data-cookies');
    var antes = escolha();
    guardar(valor);
    aviso.hidden = true;
    if (valor === 'sim') carregar();
    else if (antes === 'sim') { apagarCookiesDoGoogle(); location.reload(); }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-cookies-abrir]')) aviso.hidden = false;
  });

  if (escolha() === 'sim') carregar();
  else if (!escolha()) aviso.hidden = false;
})();
