"""Write a tiny llama-architecture GGUF with random weights (a few MB), for tests that need a
real llama-server process without a real model:

    pip install gguf numpy
    python scripts/dev/make_tiny_gguf.py tiny.gguf <layers> <embedding size>   # e.g. 6 256

The model answers with garbage; it is only good for what does not depend on the answer:
process start, health, memory figures, prompt-cache behaviour, suspend/resume, priorities.
"""
import numpy as np, sys
from gguf import GGUFWriter, GGMLQuantizationType
# a tiny llama-architecture model with a byte-level toy vocabulary; random weights (not useful output)
n_layer, n_embd, n_head, n_ff, n_ctx = int(sys.argv[2]), int(sys.argv[3]), 4, int(sys.argv[3])*2, 16384
tokens = ["<unk>", "<s>", "</s>"] + [chr(c) for c in range(32, 127)] + ["▁"] 
# SPM needs byte fallback tokens; keep simple: add <0x..> tokens
tokens += [f"<0x{b:02X}>" for b in range(256)]
n_vocab = len(tokens)
w = GGUFWriter(sys.argv[1], "llama")
w.add_name("tiny"); w.add_context_length(n_ctx); w.add_embedding_length(n_embd)
w.add_block_count(n_layer); w.add_feed_forward_length(n_ff); w.add_head_count(n_head); w.add_head_count_kv(2)
w.add_layer_norm_rms_eps(1e-5); w.add_rope_dimension_count(n_embd // n_head); w.add_file_type(0)
w.add_tokenizer_model("llama")
w.add_token_list(tokens); w.add_token_scores([0.0]*n_vocab)
types = [2,3,3] + [1]*(n_vocab-3-256) + [6]*256   # unknown, control, control, normal..., byte
w.add_token_types(types)
w.add_bos_token_id(1); w.add_eos_token_id(2); w.add_unk_token_id(0)
rng = np.random.default_rng(1)
def t(name, *shape): w.add_tensor(name, rng.standard_normal(shape).astype(np.float32)*0.02)
t("token_embd.weight", n_vocab, n_embd); w.add_tensor("output_norm.weight", np.ones(n_embd, np.float32)); t("output.weight", n_vocab, n_embd)
hd = n_embd // n_head; kvd = 2*hd
for i in range(n_layer):
    p=f"blk.{i}."
    w.add_tensor(p+"attn_norm.weight", np.ones(n_embd, np.float32)); w.add_tensor(p+"ffn_norm.weight", np.ones(n_embd, np.float32))
    t(p+"attn_q.weight", n_embd, n_embd); t(p+"attn_k.weight", kvd, n_embd); t(p+"attn_v.weight", kvd, n_embd); t(p+"attn_output.weight", n_embd, n_embd)
    t(p+"ffn_gate.weight", n_ff, n_embd); t(p+"ffn_up.weight", n_ff, n_embd); t(p+"ffn_down.weight", n_embd, n_ff)
w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()
