#!/usr/bin/env bash
# Модель реранкера (M3, Р-14) для сервиса reranker из compose.yaml.
#
#   sudo deploy/reranker/fetch-model.sh [fp32|int8] [каталог]
#
# Скачивает закреплённую ревизию cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
# (Apache 2.0, выбор ML 30.09) в формате ONNX и сверяет sha256 каждого
# файла: подменённая модель не запустится. По умолчанию — fp32, та же
# точность, что в замере ML; int8 быстрее (~1,2 с против ~1,9 с на 30
# кандидатов, 4 vCPU, замер 30.09), но баллы чуть другие — только после
# замера ML. Каталог по умолчанию — /var/lib/kronto/reranker-model
# (RERANK_MODEL_DIR в compose.yaml).
set -euo pipefail

variant="${1:-fp32}"
dir="${2:-/var/lib/kronto/reranker-model}"
repo="https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/resolve"
revision=1427fd652930e4ba29e8149678df786c240d8825

case "$variant" in
    fp32) onnx=onnx/model.onnx
          onnx_sha=3e9a03ed1e966f7c5288dd4230e3d6a9bf5e3a170a06f1f4241c5bca12c6487c ;;
    int8) onnx=onnx/model_quint8_avx2.onnx
          onnx_sha=6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b ;;
    *) echo "вариант: fp32 или int8" >&2; exit 64 ;;
esac

# Путь в репозитории модели, куда положить, sha256.
files=(
    "config.json config.json cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77"
    "tokenizer.json tokenizer.json 62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626"
    "tokenizer_config.json tokenizer_config.json e7fbfbfa6347b4e414c1cee50d142e2c2f9a895dad68b068ae83a8b564c3837e"
    "special_tokens_map.json special_tokens_map.json 378eb3bf733eb16e65792d7e3fda5b8a4631387ca04d2015199c4d4f22ae554d"
    "sentencepiece.bpe.model sentencepiece.bpe.model cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"
    # Сервис берёт ONNX только под именем onnx/model.onnx.
    "$onnx onnx/model.onnx $onnx_sha"
)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/onnx"
for entry in "${files[@]}"; do
    read -r source target sha <<<"$entry"
    curl -fsSL --retry 3 -o "$tmp/$target" "$repo/$revision/$source"
    echo "$sha  $tmp/$target" | sha256sum --check --quiet
done

# Каталог меняется целиком: сервис не увидит смесь старых и новых файлов.
mkdir -p "$(dirname "$dir")"
rm -rf "$dir.new"
mv "$tmp" "$dir.new"
chmod -R a+rX "$dir.new"
rm -rf "$dir"
mv "$dir.new" "$dir"
trap - EXIT
echo "модель ($variant) — в $dir"
