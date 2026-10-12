# catalog-audit

A small, read-only audit tool for **public** product pages. It gives you observations (title, description length, structured data, missing images) to start an honest conversation with a shop owner. It does **not** judge quality and never bypasses protection.

> Use it only on pages you are allowed to read. It obeys `robots.txt`. No logins, no forms, no changes.

## What it does
- Reads `robots.txt` first. Only a readable `200` (parsed) or a `404` (no file) lets it continue; `401/403/5xx/timeout` means "unknown" and the page is **skipped**.
- Reports: HTTP status, title, `meta description` length, length of the text inside an element marked as description (class/id contains `descr`), whether JSON-LD declares a `Product`, and the status of the first images.
- Images: `HEAD` first; if the server answers `405/501`, a tiny ranged `GET`. Only `404/410` counts as **not found**; `401/403` is **blocked** (check in a browser), not "broken". Images on other hosts are not requested unless you pass `--allow-external-images`.
- Stops at a request budget and a time budget.

## Honest limits
`None`/`false` means "not detected by this method", not "missing". A short or empty description in the output is a reason to look, not proof of a defect. Check every finding in a browser before you tell anyone about it.

## Use
```bash
PYTHONPATH=src python3 -m catalog_audit urls.txt            # one URL per line, # comments allowed
PYTHONPATH=src python3 -m catalog_audit urls.txt --max-requests 100 --max-seconds 60 --pause 3
python3 -m unittest discover -s tests         # tests use a local fake site, no internet
```
Python 3.9+, standard library only. MIT license.

---

# catalog-audit (українською)

Невеликий інструмент тільки для читання: перевіряє **публічні** сторінки товарів і дає спостереження (заголовок, довжина опису, структуровані дані, відсутні зображення) для чесної розмови з власником магазину. Він не оцінює якість і не обходить захист.

Повага до `robots.txt`: якщо файл недоступний (`401/403/5xx`, таймаут), сторінка пропускається. Відповідь `403` для зображення означає «заблоковано», а не «зламано». `None`/`false` означає «цим способом не виявлено», а не «відсутнє». Усі знахідки перевіряйте у браузері.
