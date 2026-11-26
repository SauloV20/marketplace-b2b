// Formulários fiscais: "Buscar pelo CEP" preenche rua, bairro, cidade, código IBGE e estado (ViaCEP, pelo servidor).
(function () {
  document.querySelectorAll('[data-buscar-cep]').forEach(function (botao) {
    botao.addEventListener('click', async function () {
      const form = botao.closest('form');
      const cep = (form.querySelector('[name="cep"]').value || '').replace(/\D/g, '');
      if (cep.length !== 8) { botao.textContent = 'CEP com 8 números'; return; }
      botao.textContent = 'Buscando...';
      try {
        const r = await (await fetch(botao.dataset.buscarCep.replace('00000000', cep))).json();
        if (!r.cod_municipio) { botao.textContent = 'CEP não encontrado'; return; }
        ['logradouro', 'bairro', 'municipio', 'cod_municipio', 'uf'].forEach(function (campo) {
          const el = form.querySelector('[name="' + campo + '"]');
          if (el && r[campo]) el.value = r[campo];
        });
        botao.textContent = 'Endereço preenchido';
      } catch (e) { botao.textContent = 'Não foi possível buscar'; }
    });
  });
})();
