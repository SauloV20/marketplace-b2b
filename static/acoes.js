// Ações simples das páginas (confirmar, imprimir, copiar...). Ficam aqui, e não escritas dentro do HTML,
// para a regra de segurança do navegador (CSP) poder bloquear qualquer script colocado dentro da página.
(function () {
  'use strict';
  function marcados() { return Array.prototype.slice.call(document.querySelectorAll('input[name=oid]:checked')); }

  // data-confirmar="Pergunta?" em botão/link (clique) ou em formulário (envio)
  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-confirmar]:not(form)');
    if (el && !confirm(el.getAttribute('data-confirmar'))) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    // configuração fiscal: só pergunta se o ambiente escolhido for produção
    el = e.target.closest('[data-confirmar-producao]');
    if (el && el.form && el.form.ambiente && el.form.ambiente.value === '1' &&
        !confirm(el.getAttribute('data-confirmar-producao'))) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.hasAttribute('data-confirmar') && !confirm(f.getAttribute('data-confirmar'))) { e.preventDefault(); return; }
    if (f.hasAttribute('data-excluir-marcados')) {  // lista de pedidos: excluir os marcados
      var n = marcados().length;
      if (!n) { alert('Marque os pedidos que quer excluir.'); e.preventDefault(); return; }
      if (!confirm('Excluir de vez ' + n + ' pedido(s)? Itens e documentos anexados somem, e o estoque dos pedidos ' +
                   'em aberto ou pagos volta para a loja. Não dá para desfazer.')) e.preventDefault();
    }
  });

  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-imprimir],[data-selecionar],[data-copiar],[data-abrir],[data-imprimir-marcados]');
    if (!el) return;
    if (el.hasAttribute('data-imprimir')) window.print();
    else if (el.hasAttribute('data-selecionar')) el.select();
    else if (el.hasAttribute('data-copiar')) {
      var alvo = document.querySelector(el.getAttribute('data-copiar'));
      if (alvo) navigator.clipboard.writeText(alvo.value).then(function () { el.textContent = 'Copiado!'; });
    } else if (el.hasAttribute('data-abrir')) {
      var d = document.querySelector(el.getAttribute('data-abrir'));
      if (d) d.open = true;
    } else {
      var ids = marcados().map(function (c) { return 'oid=' + encodeURIComponent(c.value); }).join('&');
      if (!ids) { alert('Marque os pedidos que quer imprimir.'); return; }
      window.open(el.getAttribute('data-imprimir-marcados') + '?' + ids, '_blank');
    }
  });

  document.addEventListener('change', function (e) {
    var el = e.target;
    if (el.hasAttribute('data-enviar-ao-mudar') && el.form) el.form.submit();
    if (el.hasAttribute('data-marcar-todos')) {
      document.querySelectorAll('input[name=oid]').forEach(function (c) { c.checked = el.checked; });
    }
  });

  // painel no celular: botão Menu abre e fecha a lista de páginas
  var menu = document.querySelector('[data-menu-painel]');
  if (menu) {
    menu.addEventListener('click', function () {
      var aberto = menu.parentNode.classList.toggle('aberto');
      menu.setAttribute('aria-expanded', aberto ? 'true' : 'false');
    });
    var atual = menu.parentNode.querySelector('a.on');
    if (atual) menu.textContent = 'Menu · ' + atual.textContent.trim();
  }

  // painel no celular: cada linha das tabelas vira um cartão com o nome da coluna ao lado do valor
  document.querySelectorAll('.main table:not(.tabela-campos)').forEach(function (t) {
    var linhas = t.rows;
    if (!linhas.length) return;
    var cab = linhas[0], nomes = [];
    for (var i = 0; i < cab.cells.length; i++) {
      var c = cab.cells[i];
      if (c.tagName !== 'TH') return;  // sem cabeçalho: deixa como está
      for (var k = 0; k < (c.colSpan || 1); k++) nomes.push(c.textContent.trim());
    }
    cab.classList.add('cab');
    for (var r = 1; r < linhas.length; r++) {
      var col = 0;
      for (var j = 0; j < linhas[r].cells.length; j++) {
        var cel = linhas[r].cells[j];
        if (cel.tagName === 'TD' && !cel.hasAttribute('data-rotulo')) cel.setAttribute('data-rotulo', nomes[col] || '');
        col += cel.colSpan || 1;
      }
    }
    t.classList.add('cartoes');
  });

  // menu da conta no celular: rola as abas até a página aberta ficar à vista
  var aba = document.querySelector('.conta-nav a.on');
  if (aba && aba.parentNode.scrollWidth > aba.parentNode.clientWidth) {
    aba.parentNode.scrollLeft = aba.offsetLeft - aba.parentNode.offsetLeft - 12;
  }
})();
