// Drag-and-drop for the About-you composer. Validation lives in the page (it knows the
// session totals); this hook just hands over the raw File list.
import { useEffect, useRef, useState, type DragEvent } from "react";

export const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".markdown"];
export const ACCEPT_ATTR = [
  ...ACCEPTED_EXTENSIONS,
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
  "text/markdown",
].join(",");

const hasFiles = (e: DragEvent | globalThis.DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");

/** Handlers to spread on a drop target, plus whether files are currently dragged over it. */
export function useFileDrop(onFiles: (files: File[]) => void, inactive: boolean) {
  const [over, setOver] = useState(false);
  const depth = useRef(0); // dragenter/leave fire for children too; count to know when we really left

  // A file dropped just outside the target would make the browser navigate away to open it. Prevent that.
  useEffect(() => {
    const block = (e: globalThis.DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    window.addEventListener("dragover", block);
    window.addEventListener("drop", block);
    return () => {
      window.removeEventListener("dragover", block);
      window.removeEventListener("drop", block);
    };
  }, []);

  const handlers = {
    onDragEnter: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current += 1;
      setOver(true);
    },
    onDragOver: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = inactive ? "none" : "copy";
    },
    onDragLeave: () => {
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setOver(false);
    },
    onDrop: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setOver(false);
      if (inactive) return;
      const files = Array.from(e.dataTransfer.files);
      if (files.length) onFiles(files);
    },
  };

  return { over, handlers };
}
