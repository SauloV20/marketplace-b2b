// Finalização: ao digitar o CEP, recalcula as opções de entrega, o frete e o total (o servidor confere de novo
// ao confirmar). Escolher outra entrega atualiza o total na hora.
(function () {
  const campo = document.getElementById('cep');
  if (!campo) return;
  const valor = document.getElementById('frete-valor'), prazo = document.getElementById('frete-prazo');
  const total = document.getElementById('total-geral'), obs = document.getElementById('frete-obs');
  const escolha = document.getElementById('frete-escolha');
  let ultimo = campo.value.replace(/\D/g, '');
  // escolher outra entrega (PAC, SEDEX, tabela própria) troca o frete e o total na hora
  if (escolha) {
    escolha.addEventListener('change', (ev) => {
      const op = ev.target;
      if (!op || op.name !== 'frete_opcao') return;
      valor.textContent = op.dataset.valor;
      valor.classList.toggle('gratis', !!op.dataset.gratis);
      prazo.textContent = op.dataset.prazo ? '(' + op.dataset.prazo + ')' : '';
      total.textContent = op.dataset.total;
    });
  }
  campo.addEventListener('input', async () => {
    let d = campo.value.replace(/\D/g, '').slice(0, 8);
    campo.value = d.length > 5 ? d.slice(0, 5) + '-' + d.slice(5) : d;
    if (d.length !== 8 || d === ultimo) return;
    ultimo = d;
    valor.textContent = 'Calculando...';
    try {
      const marcada = escolha && escolha.querySelector('input[name="frete_opcao"]:checked');
      const opcao = marcada ? '&opcao=' + encodeURIComponent(marcada.value) : '';
      const r = await (await fetch(campo.dataset.freteUrl + '?cep=' + d + opcao)).json();
      if (!r.ok) { valor.textContent = 'CEP inválido'; return; }
      valor.textContent = r.combinar ? 'A combinar' : (r.gratis ? 'Grátis' : r.valor);
      valor.classList.toggle('gratis', !!r.gratis);
      prazo.textContent = r.prazo ? '(' + r.prazo + ')' : '';
      total.textContent = r.total;
      obs.hidden = !r.combinar;
      if (escolha) escolha.innerHTML = r.opcoes || '';
      // outro estado, outro preço (formação de preço): o servidor manda o resumo dos itens refeito
      const resumo = document.getElementById('resumo-itens');
      if (r.resumo && resumo) resumo.innerHTML = r.resumo;
    } catch (e) { valor.textContent = 'Não foi possível calcular'; }
  });

  // Endereços salvos: escolher um preenche CEP e endereço (e recalcula o frete)
  const salvo = document.getElementById('endereco-salvo'), endereco = document.getElementById('endereco');
  const guardar = document.querySelector('input[name="guardar_endereco"]');
  if (salvo && endereco) {
    const aplicar = () => {
      const op = salvo.selectedOptions[0];
      if (guardar) guardar.checked = false;
      if (!op || !op.dataset.cep) { endereco.value = ''; campo.value = ''; endereco.focus(); return; }
      endereco.value = op.dataset.endereco;
      campo.value = op.dataset.cep;
      campo.dispatchEvent(new Event('input'));
    };
    salvo.addEventListener('change', aplicar);
  }
})();
