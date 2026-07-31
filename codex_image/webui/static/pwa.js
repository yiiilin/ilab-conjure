(() => {
  if (!("serviceWorker" in navigator)) return;
  if (!window.isSecureContext) return;

  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js", { scope: "/", updateViaCache: "none" }).catch(() => {
      // PWA support is opportunistic; the WebUI must still work as a normal page.
    });
  });
})();
