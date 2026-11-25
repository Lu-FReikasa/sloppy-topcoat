// Package gguf reads the metadata header of a GGUF model file: just enough to size the
// KV cache and the memory a load needs (architecture, layers, heads). Tensor data is never
// read.
package gguf

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// Meta is the part of the metadata the node uses. Zero means "not in the file".
type Meta struct {
	Architecture    string
	BlockCount      int // transformer layers
	EmbeddingLength int
	HeadCount       int
	HeadCountKV     int
	KeyLength       int // per head; 0 = EmbeddingLength / HeadCount
	ValueLength     int
	ContextLength   int // what the model was trained for
	ExpertCount     int // > 0 for mixture-of-experts models
	// PoolingType is set by embedding models (1 mean, 2 cls, 3 last token, 4 rank); 0 = none,
	// which is what a chat model has.
	PoolingType int
	// ChatTemplate is true when the file carries tokenizer.chat_template (a model made to chat).
	ChatTemplate bool

	// Sliding-window attention (Gemma 3/4 and others): most layers look only at the last
	// SlidingWindow tokens, so their KV cache is that long, not as long as the context. Which
	// layers: SWALayers (one flag per layer, true = sliding window) or SWAPattern (every n-th
	// layer is a full-attention layer, the others slide). Neither = all layers are full.
	SlidingWindow  int
	SWAPattern     int
	SWALayers      []bool
	KeyLengthSWA   int // per head of a sliding layer; 0 = same as KeyLength
	ValueLengthSWA int
	// SharedKVLayers: the last n layers reuse the KV cache of earlier ones and have none.
	SharedKVLayers int
	// HeadCountKVLayers is head_count_kv when the file gives it per layer (an array).
	HeadCountKVLayers []int
}

// HeadDimK is the key size per head.
func (m Meta) HeadDimK() int {
	if m.KeyLength > 0 {
		return m.KeyLength
	}
	if m.HeadCount > 0 {
		return m.EmbeddingLength / m.HeadCount
	}
	return 0
}

// HeadDimV is the value size per head.
func (m Meta) HeadDimV() int {
	if m.ValueLength > 0 {
		return m.ValueLength
	}
	return m.HeadDimK()
}

// swaCells is how many tokens a sliding-window layer keeps: the window for each sequence plus
// one micro-batch (llama.cpp sizes its SWA cache n_swa * sequences + n_ubatch, never more than
// the full cache).
func (m Meta) swaCells(ctx, slots int) int {
	return min(ctx, m.SlidingWindow*max(slots, 1)+512)
}

// sliding reports whether layer i is a sliding-window layer. Unknown (no window, or a window
// without a pattern) counts as full attention: too much memory is the safe error.
func (m Meta) sliding(i int) bool {
	switch {
	case m.SlidingWindow <= 0:
		return false
	case len(m.SWALayers) == m.BlockCount:
		return m.SWALayers[i]
	case m.SWAPattern > 1:
		// llama.cpp set_swa_pattern(n): n-1 sliding layers, then one full-attention layer
		return i%m.SWAPattern < m.SWAPattern-1
	}
	return false
}

// KVCacheBytes is the size of the KV cache for ctx tokens at 16-bit precision (the
// llama.cpp default), or 0 if the file does not say enough.
func (m Meta) KVCacheBytes(ctx int) int64 { return m.KVCacheBytesSlots(ctx, 1) }

// KVCacheBytesSlots is KVCacheBytes for a server with the given number of slots (ctx is the
// total over all of them). Layers with sliding-window attention count only their window; layers
// that share the cache of an earlier layer count nothing; a file that gives the KV head count
// per layer is read per layer.
func (m Meta) KVCacheBytesSlots(ctx, slots int) int64 {
	if ctx <= 0 || m.BlockCount <= 0 {
		return 0
	}
	kv := m.HeadCountKV
	if kv == 0 {
		kv = m.HeadCount
	}
	layers := m.BlockCount - m.SharedKVLayers
	if layers <= 0 {
		layers = m.BlockCount
	}
	var total int64
	for i := 0; i < layers; i++ {
		heads := kv
		if len(m.HeadCountKVLayers) == m.BlockCount && m.HeadCountKVLayers[i] > 0 {
			heads = m.HeadCountKVLayers[i]
		}
		k, v, cells := m.HeadDimK(), m.HeadDimV(), ctx
		if m.sliding(i) {
			if m.KeyLengthSWA > 0 {
				k = m.KeyLengthSWA
			}
			if m.ValueLengthSWA > 0 {
				v = m.ValueLengthSWA
			}
			cells = m.swaCells(ctx, slots)
		}
		total += int64(heads) * int64(k+v) * 2 * int64(cells)
	}
	return total
}

// ErrNotGGUF is returned for files that do not start with the GGUF magic.
var ErrNotGGUF = errors.New("not a GGUF file")

// Value types of the GGUF format.
const (
	tUint8   = 0
	tInt8    = 1
	tUint16  = 2
	tInt16   = 3
	tUint32  = 4
	tInt32   = 5
	tFloat32 = 6
	tBool    = 7
	tString  = 8
	tArray   = 9
	tUint64  = 10
	tInt64   = 11
	tFloat64 = 12
)

// maxKV bounds the number of metadata entries read (a sanity limit for corrupt files).
const maxKV = 1 << 16

// ReadFile reads the metadata of the GGUF file at path. For a split model pass the first part.
func ReadFile(path string) (Meta, error) {
	f, err := os.Open(path) //nolint:gosec // a model file of the node's own directories, never a request path
	if err != nil {
		return Meta{}, err
	}
	defer f.Close()
	return Read(f)
}

// Read reads the metadata from r.
func Read(r io.Reader) (Meta, error) {
	br := bufio.NewReaderSize(r, 1<<16)
	var magic [4]byte
	if _, err := io.ReadFull(br, magic[:]); err != nil || string(magic[:]) != "GGUF" {
		return Meta{}, ErrNotGGUF
	}
	var version uint32
	if err := binary.Read(br, binary.LittleEndian, &version); err != nil {
		return Meta{}, err
	}
	if version < 2 || version > 3 {
		return Meta{}, fmt.Errorf("gguf: unsupported version %d", version)
	}
	var tensors, kvs uint64
	if err := binary.Read(br, binary.LittleEndian, &tensors); err != nil {
		return Meta{}, err
	}
	if err := binary.Read(br, binary.LittleEndian, &kvs); err != nil {
		return Meta{}, err
	}
	if kvs > maxKV {
		return Meta{}, fmt.Errorf("gguf: %d metadata entries", kvs)
	}
	ints := map[string]int64{}
	arrays := map[string][]int64{}
	var arch string
	chatTemplate := false
	for i := uint64(0); i < kvs; i++ {
		key, err := readString(br)
		if err != nil {
			return Meta{}, err
		}
		var typ uint32
		if err := binary.Read(br, binary.LittleEndian, &typ); err != nil {
			return Meta{}, err
		}
		if typ == tString {
			s, err := readString(br)
			if err != nil {
				return Meta{}, err
			}
			if key == "tokenizer.chat_template" {
				chatTemplate = len(s) > 0
			}
			if key == "general.architecture" {
				arch = s
			}
			continue
		}
		if typ == tArray && wantedArray(arch, key) {
			vals, err := readIntArray(br)
			if err != nil {
				return Meta{}, err
			}
			arrays[key] = vals
			continue
		}
		v, isInt, err := readScalar(br, typ)
		if err != nil {
			return Meta{}, err
		}
		if isInt {
			ints[key] = v
		}
	}
	m := Meta{Architecture: arch, ChatTemplate: chatTemplate}
	get := func(name string) int { return int(ints[arch+"."+name]) }
	m.BlockCount = get("block_count")
	m.EmbeddingLength = get("embedding_length")
	m.HeadCount = get("attention.head_count")
	m.HeadCountKV = get("attention.head_count_kv")
	m.KeyLength = get("attention.key_length")
	m.ValueLength = get("attention.value_length")
	m.ContextLength = get("context_length")
	m.ExpertCount = get("expert_count")
	m.PoolingType = get("pooling_type")
	m.SlidingWindow = get("attention.sliding_window")
	m.SWAPattern = get("attention.sliding_window_pattern")
	m.KeyLengthSWA = get("attention.key_length_swa")
	m.ValueLengthSWA = get("attention.value_length_swa")
	m.SharedKVLayers = get("attention.shared_kv_layers")
	if flags := arrays[arch+".attention.sliding_window_pattern"]; len(flags) > 0 {
		for _, f := range flags {
			m.SWALayers = append(m.SWALayers, f != 0) // true = sliding window (gguf-py)
		}
	}
	if per := arrays[arch+".attention.head_count_kv"]; len(per) > 0 {
		for _, n := range per {
			m.HeadCountKVLayers = append(m.HeadCountKVLayers, int(n))
		}
		m.HeadCountKV = 0 // there is no single value: the per-layer list counts
	}
	if m.BlockCount == 0 || m.EmbeddingLength == 0 {
		return m, fmt.Errorf("gguf: no block_count / embedding_length for architecture %q", strings.TrimSpace(arch))
	}
	return m, nil
}

// wantedArray reports whether an array value is one the node reads (the rest — vocabularies,
// merges — is skipped).
func wantedArray(arch, key string) bool {
	return arch != "" && (key == arch+".attention.sliding_window_pattern" || key == arch+".attention.head_count_kv")
}

// readIntArray reads an array of integers or bools (anything else is an error: the keys that
// use it hold nothing else).
func readIntArray(r *bufio.Reader) ([]int64, error) {
	var et uint32
	var n uint64
	if err := binary.Read(r, binary.LittleEndian, &et); err != nil {
		return nil, err
	}
	if err := binary.Read(r, binary.LittleEndian, &n); err != nil {
		return nil, err
	}
	if n > 1<<12 {
		return nil, fmt.Errorf("gguf: array of %d layers", n)
	}
	out := make([]int64, 0, n)
	for i := uint64(0); i < n; i++ {
		v, isInt, err := readScalar(r, et)
		if err != nil {
			return nil, err
		}
		if !isInt {
			return nil, fmt.Errorf("gguf: array of non-integers")
		}
		out = append(out, v)
	}
	return out, nil
}

func readString(r *bufio.Reader) (string, error) {
	var n uint64
	if err := binary.Read(r, binary.LittleEndian, &n); err != nil {
		return "", err
	}
	if n > 1<<24 {
		return "", fmt.Errorf("gguf: string of %d bytes", n)
	}
	b := make([]byte, n)
	if _, err := io.ReadFull(r, b); err != nil {
		return "", err
	}
	return string(b), nil
}

func skipString(r *bufio.Reader) error {
	var n uint64
	if err := binary.Read(r, binary.LittleEndian, &n); err != nil {
		return err
	}
	_, err := r.Discard(int(min(n, 1<<31)))
	return err
}

// readScalar reads a non-string value; isInt reports whether it is an integer (or bool)
// worth keeping. Arrays (vocabularies, merges) are skipped.
func readScalar(r *bufio.Reader, typ uint32) (v int64, isInt bool, err error) {
	switch typ {
	case tUint8, tInt8, tBool:
		b, err := r.ReadByte()
		v := int64(b)
		if typ == tInt8 && v > 127 {
			v -= 256 // two's complement
		}
		return v, true, err
	case tUint16, tInt16:
		var x uint16
		err := binary.Read(r, binary.LittleEndian, &x)
		if typ == tInt16 {
			return int64(int16(x)), true, err
		}
		return int64(x), true, err
	case tUint32, tInt32:
		var x uint32
		err := binary.Read(r, binary.LittleEndian, &x)
		if typ == tInt32 {
			return int64(int32(x)), true, err
		}
		return int64(x), true, err
	case tUint64, tInt64:
		var x uint64
		err := binary.Read(r, binary.LittleEndian, &x)
		return int64(x), true, err
	case tFloat32:
		_, err := r.Discard(4)
		return 0, false, err
	case tFloat64:
		_, err := r.Discard(8)
		return 0, false, err
	case tArray:
		var et uint32
		var n uint64
		if err := binary.Read(r, binary.LittleEndian, &et); err != nil {
			return 0, false, err
		}
		if err := binary.Read(r, binary.LittleEndian, &n); err != nil {
			return 0, false, err
		}
		for i := uint64(0); i < n; i++ {
			if et == tString {
				if err := skipString(r); err != nil {
					return 0, false, err
				}
				continue
			}
			if _, _, err := readScalar(r, et); err != nil {
				return 0, false, err
			}
		}
		return 0, false, nil
	}
	return 0, false, fmt.Errorf("gguf: unknown value type %d", typ)
}

// IsEmbedding tells an embedding model (bge-m3, nomic-embed, Qwen3-Embedding) from a chat model:
// it names a pooling of the token vectors into one. A reranker (pooling 4) is not one: it needs
// the reranking mode of the server and is not served here.
func (m Meta) IsEmbedding() bool { return m.PoolingType >= 1 && m.PoolingType <= 3 }
