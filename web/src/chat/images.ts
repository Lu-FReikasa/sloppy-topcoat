import { useCallback, useEffect, useRef, useState } from "react";

/** Images given to the chat (D125). The hub re-encodes each one and keeps only that; the browser
 *  only checks what it can see before sending (type, size, how many). */
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
/** The picture formats the hub reads (chat_images.FORMATS): anything else it refuses, and the
 *  whole message with it, so the composer leaves such a file out at once (HEIC, AVIF, SVG, TIFF). */
export const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"];

/** An image chosen for the next message: what is sent (base64) and what is shown meanwhile. */
export interface DraftImage {
  key: string;
  name: string;
  /** a data URL: shown in the composer, and in the question until the hub confirms it */
  preview: string;
  /** the base64 the hub gets */
  data: string;
}

export type ImageProblem = "type" | "size" | "count" | "read";

let counter = 0;

export function readImage(file: File): Promise<DraftImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result ?? "");
      counter += 1;
      resolve({
        key: `img-${counter}`,
        name: file.name,
        preview: url,
        data: url.slice(url.indexOf(",") + 1),
      });
    };
    reader.onerror = () => reject(reader.error ?? new Error("read"));
    reader.readAsDataURL(file);
  });
}

/** The image files among `files` that may go with the message, and why any other did not. */
export function acceptable(
  files: File[],
  already: number,
): { take: File[]; problem: ImageProblem | null } {
  let problem: ImageProblem | null = null;
  const take: File[] = [];
  for (const f of files) {
    if (!IMAGE_TYPES.includes(f.type)) problem = "type";
    else if (f.size > MAX_IMAGE_BYTES) problem = "size";
    else if (already + take.length >= MAX_IMAGES) problem = "count";
    else take.push(f);
  }
  return { take, problem };
}

/** The images of the message being written: add (pasted, dropped, chosen), remove, clear. */
export function useImageDraft() {
  const [images, setImages] = useState<DraftImage[]>([]);
  const [problem, setProblem] = useState<ImageProblem | null>(null);
  // how many there are or are being read: a second paste before the first is read counts both
  const count = useRef(0);
  useEffect(() => {
    count.current = images.length;
  }, [images]);

  const add = useCallback(async (files: File[]) => {
    const { take, problem: refused } = acceptable(files, count.current);
    setProblem(refused);
    if (take.length === 0) return;
    count.current += take.length;
    try {
      const read = await Promise.all(take.map(readImage));
      setImages((now) => [...now, ...read].slice(0, MAX_IMAGES));
    } catch {
      count.current -= take.length;
      setProblem("read");
    }
  }, []);

  const remove = useCallback((key: string) => {
    setImages((now) => now.filter((i) => i.key !== key));
    setProblem(null);
  }, []);

  const clear = useCallback(() => {
    setImages([]);
    setProblem(null);
  }, []);

  return { images, problem, add, remove, clear, setImages };
}

/** The image files of a paste or a drop (a screenshot pasted from the clipboard is one). */
export function imageFiles(data: DataTransfer | null): File[] {
  if (!data) return [];
  const out: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== "file") continue;
    const f = item.getAsFile();
    if (f) out.push(f);
  }
  if (out.length === 0) out.push(...Array.from(data.files ?? []));
  return out;
}

/** The image files of a paste. Office programs (Excel, Word, LibreOffice) put a picture of the
 *  copied cells or text on the clipboard next to the text itself: then the text is what the person
 *  pastes, not the picture. A copied picture from a web page carries an <img> in its HTML. */
export function pastedImages(data: DataTransfer | null): File[] {
  if (!data) return [];
  const text = data.getData?.("text/plain") ?? "";
  const html = data.getData?.("text/html") ?? "";
  if (text.trim() !== "" && !/<img[\s>]/i.test(html)) return [];
  return imageFiles(data);
}

/** The object URL of a picture the hub keeps, loaded with the token (an <img> cannot send it). */
export function useImageUrl(load: (() => Promise<string>) | null): string | null | "failed" {
  const [url, setUrl] = useState<string | null | "failed">(null);
  useEffect(() => {
    if (!load) return;
    let alive = true;
    let made: string | null = null;
    load().then(
      (u) => {
        made = u;
        if (alive) setUrl(u);
        else URL.revokeObjectURL(u);
      },
      () => alive && setUrl("failed"),
    );
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [load]);
  return url;
}
