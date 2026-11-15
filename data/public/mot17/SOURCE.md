# Source record: MOT17 train (ground-truth person boxes and ids) — LOCAL EVALUATION ONLY
- Dataset page: https://motchallenge.net/data/MOT17/ (Milan et al., "MOT16: A Benchmark for Multi-Object Tracking", arXiv 1603.00831)
- License: Creative Commons Attribution-NonCommercial-ShareAlike 3.0 (CC BY-NC-SA 3.0), as stated on the dataset page (checked 2026-11-15); the page adds that conditions of the original source may apply for some sequences.
- Files used: MOT17Labels.zip (ground truth, public detections, seqinfo; 9.6 MB, SHA-256 0aa79322e91583369f42f17c4d79a0b145380d8732487bba59272048dc82b2b9) and the train frames from MOT16.zip (1,954,509,127 bytes, SHA-256 b944a7ddf0fbce8742a238b9717658d26a8810ab8595e94ba7b0d9ffad3a291b). MOT16 and MOT17 train contain the same seven sequences (identical seqinfo: frame rate, length, size); MOT17 adds the corrected ground truth. Only train/ was extracted.
- Sequences: MOT17-02, 04, 05, 09, 10, 11, 13 (5,316 frames). Calibration split 02/05/09, held-out 04/10/11/13 (docs/PLAN_OCCLUSION_TRACKING.md).
- Use here: scoring tracker variants (HOTA/IDF1/MOTA/IDSW via TrackEval). Processed locally only; never uploaded.
- Not allowed in this repository: frames, screenshots, crops or any image derived from these sequences. Only numbers go into docs.
