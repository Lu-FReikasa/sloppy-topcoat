package runner

import (
	"context"
	"errors"
	"testing"
)

func TestUnavailable(t *testing.T) {
	ctx := context.Background()
	u := Unavailable{RunnerType: "fake"}
	if u.Type() != "fake" || u.Health(ctx) == nil {
		t.Fatal("unavailable runner")
	}
	reason := errors.New("why")
	u.Reason = reason
	_, err := u.Load(ctx, "m", LoadOptions{})
	if !errors.Is(err, reason) {
		t.Fatal(err)
	}
	if _, err := u.ListModels(ctx); err == nil {
		t.Fatal()
	}
	if _, err := u.Loaded(ctx); err == nil {
		t.Fatal()
	}
	if _, err := u.UnloadAll(ctx); err == nil {
		t.Fatal()
	}
	if u.Unload(ctx, "m") == nil || func() error { _, e := u.Download(ctx, DownloadRequest{}); return e }() == nil || u.Delete(ctx, "m") == nil {
		t.Fatal()
	}
}
