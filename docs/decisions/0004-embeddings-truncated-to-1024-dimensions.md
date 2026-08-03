# 0004. Embeddings are truncated to 1024 dimensions and re-normalized

- Date: 2026-07-16
- Status: accepted
- Rule: `EMBEDDING_DIM` is 1024. `cluster_similarity_threshold` is tuned against truncated vectors.

## Context

`Octen-Embedding-4B.Q8_0` is 2560-dimensional and `Octen-Embedding-0.6B.f16` is
1024. Storing the full 2560 in pgvector would pin the schema to one model, and changing embedding model later would mean a migration plus a full re-embed.

Octen is not MRL-trained, so truncation preserving structure could not be assumed.

## Measured

Against the full-2560 reference:

| candidate | pearson | top-5 neighbour overlap |
|---|---|---|
| 4B truncated to 1024 | 0.976 | 88% |
| 0.6B native 1024 | 0.900 | 72% |

So the truncated 4B not only survives truncation, it beats the smaller model at the same width.

## Decision

`EMBEDDING_DIM` is 1024. The gateway truncates and re-normalizes, so any embedding model of at least 1024 dimensions drops in without a schema change.

## Consequences

Truncation shifts cosine similarities by roughly ±0.03, with a maximum observed around 0.10. `cluster_similarity_threshold` (0.82) is therefore tuned against truncated embeddings specifically, and would have to be re-tuned if the dimension or the model changed.
