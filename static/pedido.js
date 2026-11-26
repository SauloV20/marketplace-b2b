// Pix pela Efí: pergunta ao servidor a cada 5 s (por até 30 min) se o pagamento caiu e recarrega a página.
(function () {
  const el = document.querySelector('[data-pix-status]');
  if (!el) return;
  const inicio = Date.now();
  async function ver() {
    try {
      const r = await fetch(el.dataset.pixStatus, { headers: { Accept: 'application/json' } });
      if (r.ok && (await r.json()).pago) { location.reload(); return; }
    } catch (e) { /* tenta de novo */ }
    if (Date.now() - inicio < 30 * 60 * 1000) setTimeout(ver, 5000);
    else el.textContent = 'Já pagou? Recarregue a página para ver a confirmação.';
  }
  setTimeout(ver, 5000);
})();
