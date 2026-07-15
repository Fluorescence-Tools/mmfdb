# Minimal Sphinx makefile for the standalone MMFDB documentation.
# Install the docs dependencies first:  pip install -e ".[docs]"
# Then build the HTML:                   make html

SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

.PHONY: help html clean

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS)

html:
	@$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html" $(SPHINXOPTS) -W

clean:
	rm -rf "$(BUILDDIR)"
