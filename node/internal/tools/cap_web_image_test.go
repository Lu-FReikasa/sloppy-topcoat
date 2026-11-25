package tools

import (
	"encoding/base64"
	"net/http"
	"strings"
	"testing"
)

const pngBytes = "\x89PNG\r\n\x1a\n" + "rest-of-a-png"

func TestWebImageFetchesAPictureByItsSignature(t *testing.T) {
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		switch r.URL.Path {
		case "/pic.png":
			return page(200, "image/png", pngBytes), nil
		case "/lies.png":
			return page(200, "image/png", "<svg onload=alert(1)>"), nil
		case "/drawing.svg":
			return page(200, "image/svg+xml", "<svg/>"), nil
		case "/huge.jpg":
			return page(200, "image/jpeg", "\xff\xd8\xff"+strings.Repeat("x", webImageMaxBytes)), nil
		}
		return page(404, "text/plain", ""), nil
	})
	out, err := invoke(t, o, "web.image", `{"url":"https://example.org/pic.png"}`)
	if err != nil || out["content_type"] != "image/png" || out["url"] != "https://example.org/pic.png" {
		t.Fatalf("%v %v", out, err)
	}
	if b, _ := base64.StdEncoding.DecodeString(out["data"].(string)); string(b) != pngBytes {
		t.Fatalf("the bytes changed: %q", b)
	}
	for path, want := range map[string]string{
		"/lies.png": "not a PNG", "/drawing.svg": "not a PNG", "/huge.jpg": "larger than", "/none": "HTTP 404",
	} {
		if _, err := invoke(t, o, "web.image", `{"url":"https://example.org`+path+`"}`); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v", path, err)
		}
	}
	if _, err := invoke(t, o, "web.image", `{"url":"https://intranet.example.org/a.png"}`); err == nil {
		t.Error("a private address was fetched")
	}
}

func TestWebImagesSearchesTheImageCategory(t *testing.T) {
	var seen []string
	srv := searxServer(t, 200, `{"results":[
	 {"title":"Pixel  walk cycle","url":"https://example.org/post","img_src":"https://cdn.example.org/walk.png","thumbnail_src":"https://cdn.example.org/walk-s.png","resolution":"640x480","engine":"bing images"},
	 {"title":"no page","url":"ftp://x","img_src":"https://cdn.example.org/a.png"},
	 {"title":"inline","url":"https://example.org/b","img_src":"data:image/png;base64,AAAA"}]}`, &seen)
	o := Options{Web: WebOptions{SearxngURL: srv.URL}}
	out, err := invoke(t, o, "web.images", `{"query":"pixel art walk cycle","limit":3}`)
	if err != nil || out["count"] != float64(1) {
		t.Fatalf("%v %v", out, err)
	}
	hit := out["results"].([]any)[0].(map[string]any)
	if hit["image"] != "https://cdn.example.org/walk.png" || hit["page"] != "https://example.org/post" || hit["title"] != "Pixel walk cycle" || hit["thumbnail"] != "https://cdn.example.org/walk-s.png" {
		t.Fatalf("%v", hit)
	}
	if len(seen) != 1 || !strings.Contains(seen[0], "categories=images") {
		t.Fatalf("%v", seen)
	}
}

func TestWebFetchNamesThePicturesAPageDescribes(t *testing.T) {
	o := webOptions(func(*http.Request) (*http.Response, error) {
		return page(200, "text/html", `<html><body><p>Text</p>
		 <img src="/img/walk.png" alt="A pixel character walking">
		 <img src="spacer.gif" alt="">
		 <img data-src='https://cdn.example.org/run.webp' alt='Running &amp; jumping'>
		 <img src="data:image/png;base64,AAAA" alt="inline picture">
		 <img src="/img/walk.png" alt="A pixel character walking"></body></html>`), nil
	})
	out, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/post/1"}`)
	if err != nil {
		t.Fatal(err)
	}
	imgs := out["images"].([]any)
	if len(imgs) != 2 {
		t.Fatalf("%v", imgs)
	}
	first := imgs[0].(map[string]any)
	second := imgs[1].(map[string]any)
	if first["url"] != "https://example.org/img/walk.png" || first["alt"] != "A pixel character walking" ||
		second["url"] != "https://cdn.example.org/run.webp" || second["alt"] != "Running & jumping" {
		t.Fatalf("%v", imgs)
	}
}

func TestPictureCapabilitiesComeWithTheWebOnes(t *testing.T) {
	has := func(o Options, name string) bool {
		_, ok := NewRegistry(o).Get(name)
		return ok
	}
	if has(Options{}, "web.images") || has(Options{}, "web.image") {
		t.Fatal("offered without configuration")
	}
	if !has(Options{Web: WebOptions{SearxngURL: "http://10.0.0.x:8080"}}, "web.images") || !has(Options{Web: WebOptions{Fetch: true}}, "web.image") {
		t.Fatal("web.images comes with searxng_url, web.image with web_fetch")
	}
}
