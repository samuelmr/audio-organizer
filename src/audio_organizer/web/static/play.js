// Play/stop toggles: a link with data-play plays its audio in the page; clicking it
// again, or another one, stops it. Without JavaScript the links just open the audio.
(() => {
  let current = null; // { link, audio }

  const stop = () => {
    if (!current) return;
    current.audio.pause();
    current.audio.removeAttribute("src");
    current.link.textContent = "▶";
    current.link.classList.remove("playing");
    current.link.setAttribute("aria-label", current.label);
    current = null;
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-play]");
    if (!link) return;
    event.preventDefault();
    const wasPlaying = current && current.link === link;
    stop();
    if (wasPlaying) return;

    const audio = new Audio(link.href);
    const playing = { link, audio, label: link.getAttribute("aria-label") };
    current = playing;
    link.textContent = "■";
    link.classList.add("playing");
    link.setAttribute("aria-label", "Stop");
    audio.addEventListener("ended", () => { if (current === playing) stop(); });
    audio.addEventListener("error", () => {
      if (current !== playing) return;
      stop();
      link.classList.add("unplayable");
      link.title = "Your browser cannot play this file";
    });
    audio.play().catch(() => {}); // errors are reported by the error event
  });
})();
