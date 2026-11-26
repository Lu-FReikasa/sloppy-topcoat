package agent

import (
	"context"
	"encoding/json"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/schematest"
)

// fakeImages is an image runner that only remembers what was asked of it.
type fakeImages struct {
	cfg    *protocol.ImagesConfig
	closed atomic.Bool
}

func (f *fakeImages) Info() runner.ImageRuntimeInfo {
	return runner.ImageRuntimeInfo{Release: "master-1-abcdef0", Backend: "vulkan", Installed: false}
}
func (f *fakeImages) ListImageModels(context.Context) ([]runner.ImageModel, error) {
	return []runner.ImageModel{{Name: "sd-turbo-q8", SizeBytes: 10, Format: "gguf"}}, nil
}
func (f *fakeImages) LoadImage(context.Context, string) (runner.LoadedImageModel, error) {
	return runner.LoadedImageModel{}, nil
}
func (f *fakeImages) LoadedImages(context.Context) ([]runner.LoadedImageModel, error) {
	return []runner.LoadedImageModel{{Name: "sd-turbo-q8", PID: 1, Port: 2}}, nil
}
func (f *fakeImages) UnloadImage(context.Context, string) error { return nil }
func (f *fakeImages) Close(context.Context)                     { f.closed.Store(true) }

func TestTheImageRunnerFollowsConfigImagesAndIsInTheInventory(t *testing.T) {
	var made []*fakeImages
	h, hc := energySetup(t, proxyConfig, func(o *Options) {
		o.NewImageRunner = func(c *protocol.ImagesConfig) runner.ImageRunner {
			f := &fakeImages{cfg: c}
			made = append(made, f)
			return f
		}
	})
	ctx := context.Background()
	if h.agent.imagesInfo(ctx) != nil || len(made) != 0 {
		t.Fatal("no config.images: no image runner")
	}

	on := strings.Replace(proxyConfig, `"capabilities":[]`, `"images":{"enabled":true,"backend":"auto"},"capabilities":[]`, 1)
	hc.send(t, protocol.TypeConfig, configBody(t, on))
	deadline := time.Now().Add(3 * time.Second)
	for h.agent.imagesInfo(ctx) == nil {
		if time.Now().After(deadline) {
			t.Fatal("the image runner did not start")
		}
		time.Sleep(10 * time.Millisecond)
	}
	env, err := h.agent.inventory(ctx)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(env)
	if err := schematest.Validate("inventory", raw); err != nil {
		t.Fatalf("inventory with images: %v", err)
	}
	var body protocol.Inventory
	if err := env.DecodeBody(&body); err != nil {
		t.Fatal(err)
	}
	if body.Images == nil || body.Images.Release != "master-1-abcdef0" || len(body.Images.Models) != 1 || body.Images.Loaded[0] != "sd-turbo-q8" {
		t.Fatalf("%+v", body.Images)
	}

	hc.send(t, protocol.TypeConfig, configBody(t, proxyConfig)) // switched off again
	deadline = time.Now().Add(3 * time.Second)
	for h.agent.imagesInfo(ctx) != nil || !made[0].closed.Load() {
		if time.Now().After(deadline) {
			t.Fatal("switching images off must stop the image runner")
		}
		time.Sleep(10 * time.Millisecond)
	}
}
