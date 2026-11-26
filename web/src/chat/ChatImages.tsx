import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import type { ChatImageRef } from "../types";
import { IMAGE_TYPES, MAX_IMAGES, useImageUrl, type DraftImage, type ImageProblem } from "./images";

/** The composer's button that chooses image files (D125); paste and drop add to the same list. */
export function ImageAttachButton({
  onFiles,
  disabled,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        className="image-attach"
        title={t("images.attachHint", { max: MAX_IMAGES })}
        aria-label={t("images.attach")}
        disabled={disabled}
        onClick={() => input.current?.click()}
      >
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <circle cx="9" cy="10" r="1.8" fill="currentColor" />
          <path d="M4 17l5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
        </svg>
      </button>
      <input
        ref={input}
        type="file"
        accept={IMAGE_TYPES.join(",")}
        multiple
        hidden
        data-testid="image-input"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          if (files.length) onFiles(files);
        }}
      />
    </>
  );
}

/** The images that go with the next message, each with a button that takes it out again. */
export function ImageDraftStrip({
  images,
  problem,
  onRemove,
}: {
  images: DraftImage[];
  problem: ImageProblem | null;
  onRemove: (key: string) => void;
}) {
  const { t } = useTranslation();
  if (images.length === 0 && !problem) return null;
  return (
    <div className="image-draft" data-testid="image-draft">
      {images.map((img) => (
        <figure key={img.key} className="image-draft-item">
          <img src={img.preview} alt={img.name || t("images.image")} />
          <button
            type="button"
            className="image-draft-remove"
            aria-label={t("images.remove", { name: img.name || t("images.image") })}
            onClick={() => onRemove(img.key)}
          >
            ×
          </button>
        </figure>
      ))}
      {problem && (
        <p className="image-problem small" role="alert">
          {t(`images.problem.${problem}`, { max: MAX_IMAGES })}
        </p>
      )}
    </div>
  );
}

function Thumb({
  image,
  load,
  onOpen,
}: {
  image: ChatImageRef;
  load: ((id: string) => Promise<string>) | null;
  onOpen: (src: string) => void;
}) {
  const { t } = useTranslation();
  const loader = useCallback(() => load!(image.id), [load, image.id]);
  const fetched = useImageUrl(image.src || !load ? null : loader);
  const src = image.src ?? (fetched && fetched !== "failed" ? fetched : null);
  const ratio = image.width && image.height ? `${image.width} / ${image.height}` : undefined;
  if (fetched === "failed") {
    return <span className="chat-image chat-image-missing small">{t("images.missing")}</span>;
  }
  return (
    <button
      type="button"
      className="chat-image"
      style={{ aspectRatio: ratio }}
      aria-label={t("images.open")}
      disabled={!src}
      onClick={() => src && onOpen(src)}
    >
      {src && <img src={src} alt="" />}
    </button>
  );
}

/** The images of a message as thumbnails; a click shows one whole, above everything. */
export function MessageImages({
  images,
  load,
}: {
  images: ChatImageRef[];
  load: ((id: string) => Promise<string>) | null;
}) {
  const [open, setOpen] = useState<string | null>(null);
  if (images.length === 0) return null;
  return (
    <>
      <div className="chat-images" data-testid="message-images">
        {images.map((img) => (
          <Thumb key={img.id} image={img} load={load} onOpen={setOpen} />
        ))}
      </div>
      {open && <ImageViewer src={open} onClose={() => setOpen(null)} />}
    </>
  );
}

export function ImageViewer({ src, onClose }: { src: string; onClose: () => void }) {
  const { t } = useTranslation();
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      before?.focus?.();
    };
  }, [onClose]);
  // in the body: a window of the desktop may be transformed, which would hold a fixed layer
  return createPortal(
    <div
      className="image-viewer"
      role="dialog"
      aria-modal="true"
      aria-label={t("images.viewer")}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <img src={src} alt={t("images.image")} />
      <button ref={close} type="button" className="image-viewer-close" onClick={onClose}>
        {t("images.close")}
      </button>
    </div>,
    document.body,
  );
}
