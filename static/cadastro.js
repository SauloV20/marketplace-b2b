// Ao digitar um CNPJ completo, busca a empresa na Receita (pelo servidor da loja) e preenche a razão social.
(function () {
  const doc = document.querySelector('input[name=documento][data-cnpj-url]');
  if (!doc) return;
  const form = doc.form, msg = form.querySelector('.cnpj-msg'), razao = form.querySelector('input[name=razao_social]');
  let ultimo = '';
  function dizer(texto, ruim) { msg.textContent = texto; msg.hidden = !texto; msg.classList.toggle('bad', !!ruim); }
  async function consultar() {
    const numeros = doc.value.replace(/\D/g, '');
    const pj = form.querySelector('input[name=tipo][value=pj]');
    if (numeros.length !== 14 || (pj && !pj.checked) || numeros === ultimo) return;
    ultimo = numeros;
    dizer('Consultando a Receita Federal...');
    const fd = new FormData();
    fd.append('documento', numeros); fd.append('_csrf', document.querySelector('meta[name=csrf]').content);
    try {
      const r = await fetch(doc.dataset.cnpjUrl, { method: 'POST', body: fd });
      const d = await r.json();
      if (d.ok && d.verificado && razao) { razao.value = d.razao_social; }
      dizer(d.msg, !d.ok);
    } catch (e) { dizer('Não conseguimos consultar a Receita agora. Preencha a razão social.', true); }
  }
  doc.addEventListener('change', consultar);
  doc.addEventListener('input', () => { if (doc.value.replace(/\D/g, '').length === 14) consultar(); });
})();
