"use strict";

const uploadForm = document.querySelector("#object-upload");
if (uploadForm) {
  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = uploadForm.elements.file.files[0];
    const output = uploadForm.querySelector("output");
    if (!file) return;
    output.textContent = "Uploading…";
    try {
      const response = await fetch("/objects", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": uploadForm.elements.mime_type.value || file.type || "application/octet-stream",
          "X-MMFDB-Filename": encodeURIComponent(file.name),
          "X-CSRF-Token": uploadForm.elements.csrf.value,
        },
        body: file,
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
      window.location.reload();
    } catch (error) {
      output.textContent = `Upload failed: ${error.message}`;
    }
  });
}
