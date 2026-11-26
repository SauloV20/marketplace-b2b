// Busca com sugestões: enquanto o cliente digita, mostra produtos e códigos parecidos (sem preço).
(function () {
  const caixa = document.querySelector('[data-busca]');
  if (!caixa) return;
  const form = caixa.querySelector('form'), campo = form.querySelector('input[name=q]');
  const lista = caixa.querySelector('.sugestoes'), url = caixa.dataset.busca;
  let timer, ativo = -1, ultimo = '', controle;

  function fechar() { lista.hidden = true; lista.innerHTML = ''; ativo = -1; campo.setAttribute('aria-expanded', 'false'); }
  function marcar(i) {
    const itens = lista.querySelectorAll('[role=option]');
    itens.forEach((el, n) => el.setAttribute('aria-selected', n === i ? 'true' : 'false'));
    ativo = i;
    if (itens[i]) campo.setAttribute('aria-activedescendant', itens[i].id);
  }
  function item(id, href, html) {
    const li = document.createElement('li');
    li.id = id; li.setAttribute('role', 'option');
    const a = document.createElement('a'); a.href = href; a.innerHTML = html;
    li.appendChild(a); return li;
  }
  function esc(t) { const d = document.createElement('div'); d.textContent = t || ''; return d.innerHTML; }

  async function buscar(q) {
    if (controle) controle.abort();
    controle = new AbortController();
    try {
      const r = await fetch(url + '?q=' + encodeURIComponent(q), { signal: controle.signal });
      const d = await r.json();
      if (campo.value.trim() !== q) return;
      lista.innerHTML = '';
      d.itens.forEach((s, n) => {
        const li = item('sug-' + n, s.link, '<span><b>' + esc(s.nome) + '</b><small>' + esc(s.categoria) +
          (s.codigo ? ' · Cód. ' + esc(s.codigo) : '') + (s.oferta ? ' · <em>Oferta</em>' : '') + '</small></span>');
        let foto;
        if (s.foto) { foto = document.createElement('img'); foto.src = s.foto; foto.alt = ''; }
        else { foto = document.createElement('span'); foto.className = 'sem-foto'; }
        li.firstChild.insertBefore(foto, li.firstChild.firstChild);
        lista.appendChild(li);
      });
      lista.appendChild(item('sug-todos', form.action + '?q=' + encodeURIComponent(q),
        d.itens.length ? 'Ver todos os resultados para “<b>' + esc(q) + '</b>”' : 'Nenhum produto com “<b>' + esc(q) + '</b>”. Buscar mesmo assim'));
      lista.hidden = false; ativo = -1; campo.setAttribute('aria-expanded', 'true');
    } catch (e) { if (e.name !== 'AbortError') fechar(); }
  }

  campo.setAttribute('autocomplete', 'off');
  campo.setAttribute('role', 'combobox');
  campo.setAttribute('aria-autocomplete', 'list');
  campo.setAttribute('aria-controls', lista.id);
  campo.setAttribute('aria-expanded', 'false');
  campo.addEventListener('input', () => {
    const q = campo.value.trim();
    clearTimeout(timer);
    if (q.length < 2) { fechar(); ultimo = ''; return; }
    if (q === ultimo) return;
    timer = setTimeout(() => { ultimo = q; buscar(q); }, 180);
  });
  campo.addEventListener('keydown', e => {
    const itens = lista.querySelectorAll('[role=option]');
    if (lista.hidden || !itens.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); marcar((ativo + 1) % itens.length); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); marcar((ativo - 1 + itens.length) % itens.length); }
    else if (e.key === 'Escape') { fechar(); }
    else if (e.key === 'Enter' && ativo >= 0) { e.preventDefault(); location.href = itens[ativo].querySelector('a').href; }
  });
  document.addEventListener('click', e => { if (!caixa.contains(e.target)) fechar(); });
  campo.addEventListener('focus', () => { if (lista.children.length) { lista.hidden = false; campo.setAttribute('aria-expanded', 'true'); } });
})();
