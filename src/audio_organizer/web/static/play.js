// Play/stop toggles: a link with data-play plays its audio in the page; activating it
// again, or another one, stops it. With JavaScript the links act as toggle buttons
// (role=button, aria-pressed, Space works); without it they just open the audio.
(() => {
  let current = null; // { link, audio }

  const links = () => document.querySelectorAll("a[data-play]");
  links().forEach((link) => {
    link.setAttribute("role", "button");
    link.setAttribute("aria-pressed", "false");
  });

  // the icons are symbols in base.html
  const setIcon = (link, name) => {
    const use = link.querySelector("use");
    if (use) use.setAttribute("href", `#icon-${name}`);
  };

  const stop = () => {
    if (!current) return;
    current.audio.pause();
    current.audio.removeAttribute("src");
    setIcon(current.link, "play");
    current.link.classList.remove("playing");
    current.link.setAttribute("aria-pressed", "false");
    current = null;
  };

  const toggle = (link) => {
    const wasPlaying = current && current.link === link;
    stop();
    if (wasPlaying) return;

    const audio = new Audio(link.href);
    const playing = { link, audio };
    current = playing;
    setIcon(link, "stop");
    link.classList.add("playing");
    link.setAttribute("aria-pressed", "true");
    audio.addEventListener("ended", () => { if (current === playing) stop(); });
    audio.addEventListener("error", () => {
      if (current !== playing) return;
      stop();
      link.classList.add("unplayable");
      link.title = "Your browser cannot play this file";
    });
    audio.play().catch(() => {}); // errors are reported by the error event
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-play]");
    if (!link) return;
    event.preventDefault();
    toggle(link);
  });
  document.addEventListener("keydown", (event) => {
    const link = event.target.closest && event.target.closest("a[data-play]");
    if (link && event.key === " ") {
      event.preventDefault();
      toggle(link);
    }
  });
})();
