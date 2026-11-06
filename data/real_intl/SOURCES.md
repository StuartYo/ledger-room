# Foreign receipt photos (real_intl): sources

No image from these sets is in the repository. They are downloaded to a local drive and read from there by
`eval/real_photo_eval.py`. Downloaded files are only read, never executed.

## CORD v2

- Where: Hugging Face `naver-clova-ix/cord-v2`, https://huggingface.co/datasets/naver-clova-ix/cord-v2
- What: photos of Indonesian shop and restaurant receipts with field labels (`ground_truth` JSON).
- License: Creative Commons Attribution 4.0 International (CC BY 4.0).
- Credit: Park et al., "CORD: A Consolidated Receipt Dataset for Post-OCR Parsing", Document Intelligence Workshop at
  NeurIPS 2019. NAVER Clova. https://github.com/clovaai/cord
- Used: the `validation` (100) and `test` (100) splits. The `train` split is not downloaded.
- Local copy: `F:\datasets\receipts\cord_v2\data\*.parquet` (as published) and `F:\datasets\receipts\cord_v2\prepared\`
  (JPEG per receipt plus `truth.json`).
- Answer used: `gt_parse.total.total_price` from the dataset's own ground truth. Receipts without it are skipped.

## Zenodo 13688441 (Express Expense SRD, extended)

- Where: https://zenodo.org/records/13688441 (`Dataset.zip`, 19 MB)
- What: 200 hand-held photos of restaurant receipts (the ones we opened are from US restaurants), with a full-text transcription per receipt and the
  four document corners.
- License: Creative Commons Attribution 4.0 International (CC BY 4.0).
- Credit: Auad, Alves, Kakizaki, Reis, Silva. "A labeled dataset of hand-captured images of restaurant receipts",
  Zenodo, 2024. https://github.com/MaVILab-UFV/Filtering-Preparation-for-OCR_SIBGRAPI-2024
- Local copy: `F:\datasets\receipts\zenodo_13688441\` (`Dataset.zip`, `extracted\`, `prepared\`).
- Answer used: the set has no total field. `prepare-zenodo` takes the amount on the line that starts with TOTAL in the
  human transcription, and skips a receipt with no such line or with several different amounts. This is a parsed
  answer, not a label, so these results are weaker evidence than CORD.

## How to rebuild the local copies

```
pip install pyarrow     # only for prepare-cord
python -I eval/real_photo_eval.py prepare-cord --src F:\datasets\receipts\cord_v2 --dst F:\datasets\receipts\cord_v2\prepared
python -I eval/real_photo_eval.py prepare-zenodo --src F:\datasets\receipts\zenodo_13688441\extracted --dst F:\datasets\receipts\zenodo_13688441\prepared
python -I eval/real_photo_eval.py intl --root F:\datasets\receipts\cord_v2\prepared --model-dir E:\models\Qwen3.5-2B-int4-ov --device GPU
```

## What is and is not tested on these receipts

Only the total. The Taiwanese rules (UBN checksum, invoice period, duplicates, VAT Act Art. 19) do not apply to
foreign receipts and are not run. Ledger Room's own reader is not run on them either: its prompt and row labels are
written for Taiwanese vouchers, and its amount parser reads "60.000" (an Indonesian thousands separator) as 60.

What is tested is the idea behind Ledger Room's total check, written out in `eval/real_photo_eval.py` for any
currency format. Three columns: the amount on the OCR row labelled TOTAL (OCR only), the model's answer (model only),
and both together, where a total is accepted only when the two agree and otherwise goes to a person.
