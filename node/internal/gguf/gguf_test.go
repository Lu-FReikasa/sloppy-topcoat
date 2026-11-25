package gguf

import (
	"bytes"
	"encoding/binary"
	"errors"
	"os"
	"reflect"
	"testing"
)

type builder struct{ bytes.Buffer }

func (b *builder) u32(v uint32) { _ = binary.Write(b, binary.LittleEndian, v) }
func (b *builder) u64(v uint64) { _ = binary.Write(b, binary.LittleEndian, v) }
func (b *builder) str(s string) { b.u64(uint64(len(s))); b.WriteString(s) }
func (b *builder) kvString(k, v string) {
	b.str(k)
	b.u32(tString)
	b.str(v)
}
func (b *builder) kvU32(k string, v uint32) {
	b.str(k)
	b.u32(tUint32)
	b.u32(v)
}

// model builds a header like a real llama-architecture file: general keys, a float, the
// architecture keys and a big string array (the vocabulary) that must be skipped.
func model(kvs int) []byte {
	var b builder
	b.WriteString("GGUF")
	b.u32(3)
	b.u64(0)
	b.u64(uint64(kvs))
	b.kvString("general.architecture", "llama")
	b.kvString("general.name", "tiny")
	b.kvU32("llama.context_length", 4096)
	b.kvU32("llama.embedding_length", 4096)
	b.kvU32("llama.block_count", 32)
	b.kvU32("llama.attention.head_count", 32)
	b.kvU32("llama.attention.head_count_kv", 8)
	b.str("llama.rope.freq_base")
	b.u32(tFloat32)
	b.u32(0x47c35000)
	b.str("tokenizer.ggml.tokens")
	b.u32(tArray)
	b.u32(tString)
	b.u64(3)
	b.str("<unk>")
	b.str("hello")
	b.str("wörld")
	return b.Bytes()
}

func TestReadMetaAndKVSize(t *testing.T) {
	m, err := Read(bytes.NewReader(model(9)))
	if err != nil {
		t.Fatal(err)
	}
	want := Meta{Architecture: "llama", BlockCount: 32, EmbeddingLength: 4096, HeadCount: 32, HeadCountKV: 8, ContextLength: 4096}
	if !reflect.DeepEqual(m, want) {
		t.Fatalf("%+v want %+v", m, want)
	}
	// Llama-3-8B-like: 32 layers * 8 KV heads * (128+128) * 2 bytes = 128 KiB per token
	if got := m.KVCacheBytes(8192); got != 32*8*256*2*8192 {
		t.Fatalf("kv %d", got)
	}
	if m.KVCacheBytes(0) != 0 || (Meta{}).KVCacheBytes(100) != 0 {
		t.Fatal("empty meta must give 0")
	}
}

func TestReadRejectsGarbage(t *testing.T) {
	if _, err := Read(bytes.NewReader([]byte("not a model at all"))); !errors.Is(err, ErrNotGGUF) {
		t.Fatalf("got %v", err)
	}
	trunc := model(9)
	if _, err := Read(bytes.NewReader(trunc[:len(trunc)-20])); err == nil {
		t.Fatal("truncated file accepted")
	}
	var b builder
	b.WriteString("GGUF")
	b.u32(3)
	b.u64(0)
	b.u64(1)
	b.kvString("general.architecture", "llama")
	if _, err := Read(bytes.NewReader(b.Bytes())); err == nil {
		t.Fatal("file without block_count accepted")
	}
	var v1 builder
	v1.WriteString("GGUF")
	v1.u32(1)
	if _, err := Read(bytes.NewReader(v1.Bytes())); err == nil {
		t.Fatal("version 1 accepted")
	}
}

// TestReadRealFile reads a real GGUF when TOPCOAT_TEST_GGUF names one (not in CI).
func TestReadRealFile(t *testing.T) {
	path := os.Getenv("TOPCOAT_TEST_GGUF")
	if path == "" {
		t.Skip("TOPCOAT_TEST_GGUF not set")
	}
	m, err := ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%+v kv(2048)=%d", m, m.KVCacheBytes(2048))
	if m.BlockCount == 0 {
		t.Fatal("no layers")
	}
}

func (b *builder) kvArray(k string, elem uint32, vals []uint32, bools bool) {
	b.str(k)
	b.u32(tArray)
	b.u32(elem)
	b.u64(uint64(len(vals)))
	for _, v := range vals {
		if bools {
			b.WriteByte(byte(v))
		} else {
			b.u32(v)
		}
	}
}

// slidingModel is a Gemma-like file: sliding-window layers with a pattern (an integer or one
// flag per layer), their own head size, shared KV layers, KV heads per layer.
func slidingModel(extraKVs int, pattern func(b *builder, layers int), extra func(b *builder)) []byte {
	const layers = 12
	var b builder
	b.WriteString("GGUF")
	b.u32(3)
	b.u64(0)
	b.u64(uint64(9 + extraKVs))
	b.kvString("general.architecture", "gemmax")
	b.kvU32("gemmax.block_count", layers)
	b.kvU32("gemmax.embedding_length", 2048)
	b.kvU32("gemmax.attention.head_count", 8)
	b.kvU32("gemmax.attention.key_length", 256)
	b.kvU32("gemmax.attention.value_length", 256)
	b.kvU32("gemmax.attention.sliding_window", 512)
	b.kvU32("gemmax.attention.key_length_swa", 128)
	b.kvU32("gemmax.attention.value_length_swa", 128)
	pattern(&b, layers)
	extra(&b)
	return b.Bytes()
}

func TestSlidingWindowLayersKeepOnlyTheirWindow(t *testing.T) {
	// every 6th layer is full attention (an integer pattern, like Gemma 3), KV heads 4
	data := slidingModel(2,
		func(b *builder, _ int) { b.kvU32("gemmax.attention.sliding_window_pattern", 6) },
		func(b *builder) { b.kvU32("gemmax.attention.head_count_kv", 4) },
	)
	m, err := Read(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if m.SlidingWindow != 512 || m.SWAPattern != 6 || m.KeyLengthSWA != 128 {
		t.Fatalf("%+v", m)
	}
	// layers 5 and 11 are full, the other ten slide
	full := int64(2) * 4 * (256 + 256) * 2 * 16384
	slide := int64(10) * 4 * (128 + 128) * 2 * int64(512+512) // window + one micro-batch
	if got := m.KVCacheBytes(16384); got != full+slide {
		t.Fatalf("kv %d, want %d", got, full+slide)
	}
	naive := int64(12) * 4 * (256 + 256) * 2 * 16384
	if m.KVCacheBytes(16384)*4 > naive {
		t.Fatalf("the sliding layers did not shrink the cache: %d of %d", m.KVCacheBytes(16384), naive)
	}
	// two slots: each sequence keeps its own window; the cache is never larger than the context
	if got := m.KVCacheBytesSlots(32768, 2); got != int64(2)*4*512*2*32768+int64(10)*4*256*2*int64(2*512+512) {
		t.Fatalf("two slots: %d", got)
	}
	if m.KVCacheBytes(300) != int64(12)*4*(128+128)*2*300-int64(2)*4*(128+128)*2*300+int64(2)*4*512*2*300 {
		t.Fatalf("a short context is no longer than the context: %d", m.KVCacheBytes(300))
	}
}

func TestPerLayerFlagsHeadCountsAndSharedLayers(t *testing.T) {
	flags := []uint32{1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0} // true = sliding window (gguf-py)
	kvHeads := []uint32{2, 2, 4, 2, 2, 4, 2, 2, 4, 2, 2, 4}
	data := slidingModel(3,
		func(b *builder, _ int) { b.kvArray("gemmax.attention.sliding_window_pattern", tBool, flags, true) },
		func(b *builder) {
			b.kvArray("gemmax.attention.head_count_kv", tUint32, kvHeads, false)
			b.kvU32("gemmax.attention.shared_kv_layers", 4)
		},
	)
	m, err := Read(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if len(m.SWALayers) != 12 || m.SWALayers[2] || !m.SWALayers[0] || len(m.HeadCountKVLayers) != 12 || m.SharedKVLayers != 4 {
		t.Fatalf("%+v", m)
	}
	// only the first 8 layers own a cache: slide at 0,1,3,4,6,7 (2 heads), full at 2,5 (4 heads)
	want := int64(6)*2*(128+128)*2*1024 + int64(2)*4*(256+256)*2*8192
	if got := m.KVCacheBytes(8192); got != want {
		t.Fatalf("kv %d, want %d", got, want)
	}
}

func TestAWindowWithoutAPatternCountsAsFullAttention(t *testing.T) {
	data := slidingModel(1, func(*builder, int) {}, func(b *builder) { b.kvU32("gemmax.attention.head_count_kv", 4) })
	m, err := Read(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	// unknown which layers slide: the safe error is too much memory, not too little
	if got, want := m.KVCacheBytes(4096), int64(12)*4*(256+256)*2*4096; got != want {
		t.Fatalf("kv %d, want %d", got, want)
	}
}

func (b *builder) kvF32(k string, bits uint32) {
	b.str(k)
	b.u32(tFloat32)
	b.u32(bits)
}

// gemma4File builds the header of gemma-4-12B-it-QAT-Q4_0 with the keys and shapes the maintainer
// read from the real GGUF (first hardware test of E3-01e): 48 layers, head_count_kv one value per
// layer (8 for the sliding layers, 1 for the full ones), key/value length 512 for the full layers
// and 256 for the sliding ones, window 1024, a bool per layer (true = sliding window; 40 of 48,
// false at positions 5, 11, …), no shared KV layers. embedding_length is not among the keys he
// sent; 3840 is a placeholder (the reader needs one, the cache size does not depend on it).
func gemma4File() []byte {
	var b builder
	b.WriteString("GGUF")
	b.u32(3)
	b.u64(0)
	b.u64(15)
	b.kvString("general.architecture", "gemma4")
	b.kvU32("gemma4.block_count", 48)
	b.kvU32("gemma4.context_length", 262144)
	b.kvU32("gemma4.embedding_length", 3840)
	b.kvU32("gemma4.attention.head_count", 16)
	kv, pattern := make([]uint32, 48), make([]uint32, 48)
	for i := range kv {
		if i%6 == 5 { // positions 5, 11, …: full attention
			kv[i], pattern[i] = 1, 0
		} else {
			kv[i], pattern[i] = 8, 1
		}
	}
	b.kvArray("gemma4.attention.head_count_kv", tUint32, kv, false)
	b.kvU32("gemma4.attention.key_length", 512)
	b.kvU32("gemma4.attention.value_length", 512)
	b.kvU32("gemma4.attention.key_length_swa", 256)
	b.kvU32("gemma4.attention.value_length_swa", 256)
	b.kvU32("gemma4.attention.sliding_window", 1024)
	b.kvArray("gemma4.attention.sliding_window_pattern", tBool, pattern, true)
	b.kvU32("gemma4.attention.shared_kv_layers", 0)
	b.kvF32("gemma4.rope.freq_base", 0x49742400)     // 1e6
	b.kvF32("gemma4.rope.freq_base_swa", 0x461c4000) // 1e4
	return b.Bytes()
}

func TestGemma4ShapesFromTheRealFile(t *testing.T) {
	m, err := Read(bytes.NewReader(gemma4File()))
	if err != nil {
		t.Fatal(err)
	}
	if m.Architecture != "gemma4" || m.BlockCount != 48 || m.HeadCount != 16 || m.ContextLength != 262144 ||
		m.KeyLength != 512 || m.ValueLength != 512 || m.KeyLengthSWA != 256 || m.ValueLengthSWA != 256 ||
		m.SlidingWindow != 1024 || m.SharedKVLayers != 0 || m.HeadCountKV != 0 {
		t.Fatalf("%+v", m)
	}
	full := 0
	for i := 0; i < 48; i++ {
		if !m.SWALayers[i] {
			full++
			if i%6 != 5 || m.HeadCountKVLayers[i] != 1 {
				t.Fatalf("layer %d should be a full layer with 1 KV head: %+v", i, m)
			}
		} else if m.HeadCountKVLayers[i] != 8 {
			t.Fatalf("layer %d: %d KV heads", i, m.HeadCountKVLayers[i])
		}
	}
	if full != 8 {
		t.Fatalf("%d full-attention layers, want 8 (40 of 48 slide)", full)
	}
	// per layer: kv heads × (key + value length) × 2 bytes × cells; sliding layers keep only
	// window + 512 tokens, full layers the whole context
	const ctx = 16384
	wantFull := int64(8) * 1 * (512 + 512) * 2 * ctx
	wantSlide := int64(40) * 8 * (256 + 256) * 2 * (1024 + 512)
	if got := m.KVCacheBytes(ctx); got != wantFull+wantSlide {
		t.Fatalf("kv %d, want %d (%d MiB)", got, wantFull+wantSlide, (wantFull+wantSlide)>>20)
	}
	// what the node estimated before: head_count_kv unreadable (an array), so all 16 heads at the
	// full length in every layer — the "KV cache 24576 MB" that refused the load
	old := Meta{BlockCount: 48, EmbeddingLength: 3840, HeadCount: 16, KeyLength: 512, ValueLength: 512}
	if got := old.KVCacheBytes(ctx) >> 20; got != 24576 {
		t.Fatalf("the old estimate was %d MiB", got)
	}
	if now := m.KVCacheBytes(ctx) >> 20; now > 1000 {
		t.Fatalf("the sliding-window estimate is still %d MiB", now)
	}
}

func TestPoolingTypeMarksAnEmbeddingModel(t *testing.T) {
	build := func(pooling uint32, withKey bool) Meta {
		var b builder
		b.WriteString("GGUF")
		b.u32(3)
		b.u64(0)
		kvs := uint64(4)
		if withKey {
			kvs++
		}
		b.u64(kvs)
		b.kvString("general.architecture", "bert")
		b.kvU32("bert.embedding_length", 1024)
		b.kvU32("bert.block_count", 24)
		b.kvU32("bert.attention.head_count", 16)
		if withKey {
			b.kvU32("bert.pooling_type", pooling)
		}
		m, err := Read(bytes.NewReader(b.Bytes()))
		if err != nil {
			t.Fatal(err)
		}
		return m
	}
	if !build(2, true).IsEmbedding() { // cls pooling, like bge-m3
		t.Fatal("pooling 2 is an embedding model")
	}
	if build(0, false).IsEmbedding() || build(0, true).IsEmbedding() {
		t.Fatal("no pooling is a chat model")
	}
	if build(4, true).IsEmbedding() {
		t.Fatal("a reranker is not served as an embedding model")
	}
}

func TestTheChatTemplateIsSeenAndAnEmbeddingFileHasNone(t *testing.T) {
	withTemplate := func(template string) []byte {
		var b builder
		b.WriteString("GGUF")
		b.u32(3)
		b.u64(0)
		b.u64(5)
		b.kvString("general.architecture", "qwen3")
		b.kvU32("qwen3.block_count", 4)
		b.kvU32("qwen3.embedding_length", 64)
		b.kvU32("qwen3.context_length", 40960)
		b.kvString("tokenizer.chat_template", template)
		return b.Bytes()
	}
	chat, err := Read(bytes.NewReader(withTemplate("{{ messages }}")))
	if err != nil || !chat.ChatTemplate || chat.ContextLength != 40960 {
		t.Fatalf("chat model: %+v err %v", chat, err)
	}
	empty, err := Read(bytes.NewReader(withTemplate("")))
	if err != nil || empty.ChatTemplate {
		t.Fatalf("an empty template is none: %+v err %v", empty, err)
	}
	plain, err := Read(bytes.NewReader(model(9)))
	if err != nil || plain.ChatTemplate {
		t.Fatalf("a file without the key has none: %+v err %v", plain, err)
	}
}
