// Mostra as fotos escolhidas antes de enviar (e avisa se alguma passa de 2 MB ou se são mais de 8).
(function () {
  document.querySelectorAll('input[type=file][data-previa]').forEach(input => {
    const caixa = document.getElementById(input.dataset.previa);
    input.addEventListener('change', () => {
      caixa.innerHTML = '';
      const arquivos = [...input.files];
      if (arquivos.length > 8) {
        caixa.innerHTML = '<p class="msg">Escolha no máximo 8 fotos.</p>';
        return;
      }
      arquivos.forEach((arq, i) => {
        const fig = document.createElement('figure');
        const legenda = document.createElement('figcaption');
        const tamanho = arq.size < 1024 * 1024 ? Math.max(1, Math.round(arq.size / 1024)) + ' KB'
          : (arq.size / 1024 / 1024).toFixed(1).replace('.', ',') + ' MB';
        legenda.textContent = (i === 0 ? 'Principal · ' : '') + tamanho;
        if (arq.size > 2 * 1024 * 1024) { legenda.textContent += ' (grande demais)'; fig.classList.add('ruim'); }
        const img = document.createElement('img');
        img.alt = arq.name;
        const leitor = new FileReader();
        leitor.onload = e => { img.src = e.target.result; };
        leitor.readAsDataURL(arq);
        fig.append(img, legenda);
        caixa.append(fig);
      });
    });
  });
})();
