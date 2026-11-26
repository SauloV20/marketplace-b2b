(function () {
  const toast = document.getElementById('toast');
  const badges = document.querySelectorAll('#cart-badge, [data-cart-badge]');
  let timer;
  function say(msg, bad) {
    if (!toast) return;
    toast.textContent = msg; toast.classList.toggle('bad', !!bad); toast.classList.add('on');
    clearTimeout(timer); timer = setTimeout(() => toast.classList.remove('on'), 2600);
  }
  async function add(pid, qty) {
    const fd = new FormData(); fd.append('pid', pid); fd.append('qty', qty || 1);
    fd.append('_csrf', document.querySelector('meta[name=csrf]').content);
    try {
      const r = await fetch('/carrinho/add', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.login) { location.href = d.login; return; }
      if (d.ok) badges.forEach(function (b) { b.textContent = d.count; b.hidden = false; });
      say(d.msg, !d.ok);
    } catch (e) { say('Não foi possível adicionar. Tente de novo.', true); }
  }
  // botão "Adicionar" dos cards da vitrine
  document.querySelectorAll('[data-add]').forEach(b => b.addEventListener('click', e => {
    e.preventDefault(); add(b.dataset.add, 1);
  }));
  // formulário com quantidade da página do produto
  document.querySelectorAll('[data-add-form]').forEach(f => f.addEventListener('submit', e => {
    e.preventDefault(); add(f.dataset.addForm, f.querySelector('[name=qty]').value);
  }));
  // carrinho: um botão atualiza todas as quantidades; ao mudar alguma, ele fica em destaque
  const botaoCarrinho = document.querySelector('[data-carrinho-botao]');
  if (botaoCarrinho) {
    const aviso = document.querySelector('[data-carrinho-aviso]');
    document.querySelectorAll('input[form="carrinho-qtd"]').forEach(i => i.addEventListener('input', () => {
      botaoCarrinho.classList.remove('ghost'); botaoCarrinho.classList.add('destaque');
      if (aviso) aviso.hidden = false;
    }));
  }
  // filtros da vitrine: ordenar e "só com estoque" aplicam na hora (preço continua no botão Aplicar)
  document.querySelectorAll('[data-auto-filtro]').forEach(f => f.querySelectorAll('select, input[type=checkbox]')
    .forEach(c => c.addEventListener('change', () => f.submit())));
  // "Salvar em uma lista": o nome só aparece quando escolhe "Nova lista"
  document.querySelectorAll('[data-lista-escolha]').forEach(s => {
    const nome = s.form.querySelector('[data-lista-nova]');
    const mostrar = () => { nome.hidden = s.value !== 'nova'; nome.querySelector('input').required = s.value === 'nova'; };
    s.addEventListener('change', mostrar); mostrar();
  });
})();
