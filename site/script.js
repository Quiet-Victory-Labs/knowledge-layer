"use strict";

const copyButton = document.querySelector("#copy-commands");
const commandBlock = document.querySelector("#install-commands");
const copyStatus = document.querySelector("#copy-status");

if (copyButton && commandBlock && copyStatus) {
  copyButton.hidden = false;
  let resetTimer;

  copyButton.addEventListener("click", async () => {
    clearTimeout(resetTimer);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(commandBlock.textContent.trim());
      copyButton.querySelector("span").textContent = "Copied";
      copyStatus.textContent = "Setup commands copied.";
      resetTimer = setTimeout(() => {
        copyButton.querySelector("span").textContent = "Copy commands";
        copyStatus.textContent = "";
      }, 2500);
    } catch {
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(commandBlock);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      copyStatus.textContent = "Commands selected. Copy with Ctrl+C or ⌘C.";
    }
  });
}
