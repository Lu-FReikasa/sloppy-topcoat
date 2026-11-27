package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/voice"
)

// longSpeaker answers with a silent 16 kHz WAV of the given number of samples, standing in for
// Piper. UNVERIFIED on real hardware: real Piper audio was not sent through a node.
type longSpeaker struct{ samples int }

func (longSpeaker) Voices() []string { return []string{"long"} }
func (s longSpeaker) Speak(context.Context, string, string) ([]byte, error) {
	return voice.SilentWAV(s.samples), nil
}

// bulkCap returns n bytes of text, more than the default output limit allows.
type bulkCap struct{ n int }

func (bulkCap) Name() string                 { return "system.bulk" }
func (bulkCap) InputSchema() json.RawMessage { return json.RawMessage(`{"type":"object"}`) }
func (bulkCap) Info() capability.Info {
	return capability.Info{Description: "test", ReadOnly: true}
}
func (b bulkCap) Invoke(context.Context, json.RawMessage) (json.RawMessage, error) {
	return json.Marshal(strings.Repeat("x", b.n))
}

// greedyCap asks for more than the ceiling any capability may send.
type greedyCap struct{ bulkCap }

func (greedyCap) Name() string { return "system.greedy" }
func (greedyCap) Info() capability.Info {
	return capability.Info{Description: "test", ReadOnly: true, MaxOutputBytes: 64 << 20}
}

func outputSetup(t *testing.T, samples int) *hubConn {
	t.Helper()
	_, hc, _ := toolsSetup(t, toolsConfig(`"voice.speak","system.bulk","system.greedy"`, `"capability.invoke"`), func(o *Options) {
		o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
			reg := fake.NewToolsRegistry()
			if err := voice.Register(reg, nil, longSpeaker{samples: samples}); err != nil {
				t.Fatal(err)
			}
			for _, c := range []capability.Capability{bulkCap{n: defaultOutputLimit}, greedyCap{bulkCap{n: capability.MaxOutputCeiling}}} {
				if err := reg.Register(c); err != nil {
					t.Fatal(err)
				}
			}
			return reg
		}
	})
	return hc
}

// A spoken answer of more than about 4.4 s used to be refused as larger than the node sends
// (#214): voice.speak has its own output limit, sized for MaxSpeechBytes.
func TestALongSpokenAnswerReachesTheHub(t *testing.T) {
	for _, c := range []struct {
		name    string
		samples int
	}{
		{"20 seconds", 16000 * 20},
		{"the longest speech", (voice.MaxSpeechBytes - 44) / 2},
	} {
		t.Run(c.name, func(t *testing.T) {
			hc := outputSetup(t, c.samples)
			res := invokeCap(t, hc, "voice.speak", `{"text":"Guten Abend"}`)
			if !res.OK {
				t.Fatalf("%+v", res.Error)
			}
			raw, _ := json.Marshal(res.Result)
			var body struct {
				Output struct {
					Audio string `json:"audio_base64"`
				}
				Bytes int
			}
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Fatal(err)
			}
			wav, err := base64.StdEncoding.DecodeString(body.Output.Audio)
			if err != nil || len(wav) != 44+2*c.samples || body.Bytes <= 270_000 {
				t.Fatalf("wav %d bytes (%v), output %d bytes", len(wav), err, body.Bytes)
			}
		})
	}
}

// Every other capability keeps the default limit, and no capability can raise its own limit
// above the ceiling.
func TestOtherCapabilitiesKeepTheDefaultOutputLimit(t *testing.T) {
	hc := outputSetup(t, 16000)
	for _, name := range []string{"system.bulk", "system.greedy"} {
		res := invokeCap(t, hc, name, `{}`)
		if res.OK || res.Error.Code != protocol.ErrCapabilityFailed || !strings.Contains(res.Error.Message, "larger than this node sends") {
			t.Fatalf("%s: %+v", name, res)
		}
	}
}
