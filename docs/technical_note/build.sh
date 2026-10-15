#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

# Build the technical note PDF with IFE brand styling (requires pandoc >= 2.19 and XeLaTeX).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
BRAND="$(pwd)/brand/"
# Minimal TeX installations may lack lmodern.sty; a stub is provided (fonts are set explicitly).
kpsewhich lmodern.sty >/dev/null 2>&1 || export TEXINPUTS="$(pwd)/latex/compat:${TEXINPUTS:-}"
sed "s|@BRAND@|${BRAND}|g" latex/header.tex    > build/header.gen.tex
sed "s|@BRAND@|${BRAND}|g" latex/titlepage.tex > build/titlepage.gen.tex
pandoc DataHub_technical_note.md -o build/DataHub_technical_note.pdf \
  --pdf-engine=xelatex --number-sections \
  -H build/header.gen.tex -B build/titlepage.gen.tex \
  -V documentclass=article -V classoption=table -V fontsize=10pt -V papersize=a4 \
  -V geometry:top=26mm -V geometry:bottom=24mm -V geometry:left=24mm -V geometry:right=22mm \
  -V linkcolor=ifepurple -V urlcolor=ifered -V toccolor=ifepurple -V colorlinks=true \
  --resource-path=. -M lang=en-GB ${PANDOC_EXTRA:-}
echo "written build/DataHub_technical_note.pdf"
