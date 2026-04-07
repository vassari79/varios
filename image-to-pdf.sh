#!/usr/bin/env bash
# image-to-pdf.sh
# Converts all images in subdirectories of ~/temp/h.temp/ to PDF,
# merges them per directory, with up to 6 parallel jobs.
# Directories are moved to a .processing/ buffer while being worked on.

set -euo pipefail

SRC_DIR="/home/vassari/temp/h.temp"
BUF_DIR="/home/vassari/temp/h.temp/.processing"
MAX_JOBS=6

mkdir -p "$BUF_DIR"

notify() { command -v notify-send &>/dev/null && notify-send "image-to-pdf" "$1" || echo "$1" >&2; }

short_name() {
    local name="$1"
    local len=${#name}
    if [[ $len -le 35 ]]; then
        echo "$name"
    else
        echo "${name:0:15}…${name: -20}"
    fi
}
export -f short_name

process_dir() {
    local src="$1"
    local name
    name=$(basename "$src")
    local short
    short=$(short_name "$name")
    local work="${BUF_DIR}/${short}"

    # Rename folder and move to processing buffer
    mv "$src" "$work"

    # Convert all images (jpg, jpeg, png, webp) to PDF in parallel
    find "$work" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) -print0 \
        | xargs -0 -P "${MAX_JOBS}" -I{} bash -c '
            f="{}"
            convert -density 300 "$f" "${f%.*}.pdf" && rm -f "$f"
        '

    # Merge all PDFs into one file named after the short name
    local out="${SRC_DIR}/${short}.pdf"
    shopt -s nullglob
    local pdfs=("$work"/*.pdf)
    if [[ ${#pdfs[@]} -gt 0 ]]; then
        pdftk "${pdfs[@]}" cat output "$out"
        notify "Done: ${short}.pdf"
    else
        notify "No images found in: ${short}"
    fi

    # Clean up buffer directory
    rm -rf "$work"
}

export -f process_dir
export MAX_JOBS SRC_DIR BUF_DIR

# Find all subdirs (excluding the .processing buffer itself), process up to MAX_JOBS at a time
find "$SRC_DIR" -mindepth 1 -maxdepth 1 -type d -not -name '.processing' -print0 \
    | xargs -0 -P "$MAX_JOBS" -I{} bash -c 'process_dir "$@"' _ {}
