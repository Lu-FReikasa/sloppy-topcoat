import { useEffect, useState } from "react";

/** Loads a picture the hub keeps (with the token) and gives an object URL. */
export type ImageLoader = (path: string) => Promise<string>;

/** A picture of the hub: loaded with the token, shown when it is there; while it loads, and in the
 *  export (no token, no script), its caption stands in its place. */
export function HubImage({ path, alt, load }: { path: string; alt: string; load?: ImageLoader }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!load) return;
    let live = true;
    let made: string | null = null;
    load(path).then(
      (u) => {
        made = u;
        if (live) setUrl(u);
        else URL.revokeObjectURL(u);
      },
      () => undefined,
    );
    return () => {
      live = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [path, load]);
  return (
    <figure className="report-figure">
      {url ? <img src={url} alt={alt} /> : <div className="report-figure-wait">{alt || "…"}</div>}
      {alt && url && <figcaption>{alt}</figcaption>}
    </figure>
  );
}
