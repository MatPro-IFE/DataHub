# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""File-extension to field-reader mapping (kept free of numpy so the schema loader stays light)."""

EXTENSIONS = {
    ".vtk": "vtk", ".vtu": "vtk", ".vtp": "vtk", ".vts": "vtk", ".vtr": "vtk", ".vti": "vtk",
    ".npy": "npy", ".npz": "npy",
    ".h5": "hdf5", ".hdf5": "hdf5", ".hdf": "hdf5", ".he5": "hdf5",
    ".xdmf": "xdmf", ".xmf": "xdmf",
    ".exo": "exodus", ".e": "exodus", ".ex2": "exodus", ".exii": "exodus", ".gen": "exodus",
}
READERS = ("auto", "vtk", "npy", "hdf5", "xdmf", "exodus")
