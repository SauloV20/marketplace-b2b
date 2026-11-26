// Abre e fecha a janela de perguntas frequentes; abrir uma pergunta fecha as outras.
(function () {
  const raiz = document.getElementById('ajuda');
  if (!raiz) return;
  const janela = document.getElementById('ajuda-janela');
  const abrir = raiz.querySelector('.chat-abrir');
  function alternar(aberto) {
    janela.hidden = !aberto;
    abrir.setAttribute('aria-expanded', String(aberto));
    raiz.classList.toggle('aberto', aberto);
    if (aberto) janela.querySelector('summary')?.focus(); else abrir.focus();
  }
  abrir.addEventListener('click', () => alternar(true));
  raiz.querySelector('.chat-fechar').addEventListener('click', () => alternar(false));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !janela.hidden) alternar(false); });
  const itens = [...janela.querySelectorAll('details')];
  itens.forEach(d => d.addEventListener('toggle', () => {
    if (d.open) itens.forEach(o => { if (o !== d) o.open = false; });
  }));
})();
