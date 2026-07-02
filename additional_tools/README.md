# additional_tools

Repro scripts for the dbank / Hebrew-PDF benchmarks (not part of the service).
Run from the `file-to-markdown-convertor/` root with the factory on the path, e.g.:

```
env PYTHONPATH=<repo_root> LLM_FACTORY_PROVIDER=vertex_ai/gemini-3.5-flash \
  uv run --with litellm --with google-genai --with numpy python additional_tools/<script>.py
```

| script | what it does |
|---|---|
| `probe_detect.py` | docling table detection: cheap (do_table_structure=False) vs full — proves detection is ~free |
| `bench_tatr.py` | Microsoft Table Transformer (TATR) vs docling-layout table detection (TATR rejected) |
| `docling_chunk_test.py` | docling HybridChunker on a Hebrew finance slice (shreds tables / reverses Hebrew) |
| `compare_chunkers.py` | table-integrity: hierarchical vs markdown_table_aware (broken-table fragments) |
| `eval_retrieval.py` | golden-question retrieval eval (hit@k/MRR) — hierarchical vs hybrid, per doc (`duah`/`368`) |
| `gen_dbank_slice.py` | generate dbank markdown for a page slice (fixtures for the evals) |
| `validate_dbank.py` | dbank routing counts + serial-vs-parallel VLM timing |
| `probe_thinking.py` | VLM latency: thinking on vs off, per model |
| `run_dbank_full.py` | full-document dbank conversion timing (GPU vs 1-CPU) |
