# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for datahub_sdk.readers. Files are written with independent tools (h5py, meshio, scipy)."""
import numpy as np
import pytest

from datahub_sdk.readers import ReaderError, companions, read_snapshots, reader_for

N, T = 40, 4
PTS = np.c_[np.linspace(0, 1, N), np.zeros(N), np.zeros(N)]
CELLS = [("line", np.c_[np.arange(N - 1), np.arange(1, N)])]


def temp(t):
    return 300.0 + 10 * t + PTS[:, 0] * 50


def vec(t):
    return np.c_[PTS[:, 0] * t, np.ones(N) * t, np.zeros(N)]


def test_extension_mapping():
    assert reader_for("a/b.XDMF") == "xdmf"
    assert reader_for("x.exo") == "exodus"
    assert reader_for("x.h5") == "hdf5"
    assert reader_for("x.unknown") is None
    assert reader_for("x.unknown", "hdf5") == "hdf5"


def test_hdf5_single_and_time_axis(tmp_path):
    h5py = pytest.importorskip("h5py")
    p = tmp_path / "snap.h5"
    with h5py.File(p, "w") as f:
        f["results/temperature"] = temp(1)
        f["results/velocity"] = vec(1)
        f["mesh/ids"] = np.arange(N)
    s = read_snapshots(p)
    assert len(s) == 1 and s[0][0] is None
    assert np.allclose(s[0][1]["temperature"], temp(1))
    assert np.allclose(s[0][1]["velocity"], np.linalg.norm(vec(1), axis=1))
    s = read_snapshots(p, options={"hdf5": {"paths": ["results/*"]}})
    assert set(s[0][1]) == {"temperature", "velocity"}

    q = tmp_path / "series.hdf5"
    with h5py.File(q, "w") as f:
        f["temperature"] = np.stack([temp(t) for t in range(T)])
        f["time"] = np.arange(T) * 0.5
    s = read_snapshots(q, options={"hdf5": {"time_axis": 0, "time_path": "time"}})
    assert [t for t, _ in s] == [0.0, 0.5, 1.0, 1.5]
    assert np.allclose(s[3][1]["temperature"], temp(3))


def test_xdmf_meshio_time_series(tmp_path, monkeypatch):
    meshio = pytest.importorskip("meshio")
    monkeypatch.chdir(tmp_path)              # meshio writes the .h5 next to the working directory
    p = tmp_path / "out.xdmf"
    with meshio.xdmf.TimeSeriesWriter(str(p)) as w:
        w.write_points_cells(PTS, CELLS)
        for t in range(T):
            w.write_data(t * 0.25, point_data={"temperature": temp(t), "velocity": vec(t)})
    assert companions(p) == ["out.h5"]
    s = read_snapshots(p)
    assert [t for t, _ in s] == [0.0, 0.25, 0.5, 0.75]
    assert np.allclose(s[2][1]["temperature"], temp(2))
    assert np.allclose(s[2][1]["velocity"], np.linalg.norm(vec(2), axis=1))
    assert np.allclose(read_snapshots(p, names=["temp*"])[0][1]["temperature"], temp(0))
    assert set(read_snapshots(p, names=["temp*"])[0][1]) == {"temperature"}


def test_xdmf_inline_xml(tmp_path):
    vals = " ".join(str(v) for v in temp(0))
    p = tmp_path / "inline.xmf"
    p.write_text(f"""<?xml version="1.0"?><Xdmf Version="3.0"><Domain>
<Grid Name="g" GridType="Uniform"><Time Value="2.5"/>
<Attribute Name="temperature" Center="Node"><DataItem Format="XML" Dimensions="{N}">{vals}</DataItem></Attribute>
</Grid></Domain></Xdmf>""")
    s = read_snapshots(p)
    assert s[0][0] == 2.5 and np.allclose(s[0][1]["temperature"], temp(0))


def test_xdmf_missing_heavy_data(tmp_path):
    p = tmp_path / "x.xdmf"
    p.write_text("""<Xdmf><Domain><Grid><Attribute Name="a"><DataItem Format="HDF" Dimensions="3">gone.h5:/a</DataItem>
</Attribute></Grid></Domain></Xdmf>""")
    with pytest.raises(ReaderError):
        read_snapshots(p)


def test_exodus_meshio_netcdf4(tmp_path):
    meshio = pytest.importorskip("meshio")
    pytest.importorskip("netCDF4")
    p = tmp_path / "run.exo"
    m = meshio.Mesh(PTS, CELLS, point_data={"temperature": temp(1)})
    meshio.write(str(p), m)
    s = read_snapshots(p)
    assert len(s) >= 1
    assert np.allclose(s[-1][1]["temperature"], temp(1))


def test_exodus_netcdf3_time_series(tmp_path):
    from scipy.io import netcdf_file
    p = tmp_path / "series.e"
    f = netcdf_file(str(p), "w")
    f.createDimension("time_step", None)
    f.createDimension("num_nodes", N)
    f.createDimension("num_nod_var", 2)
    f.createDimension("len_name", 33)
    f.createDimension("num_el_blk", 2)
    f.createDimension("num_el_in_blk1", 3)
    f.createDimension("num_el_in_blk2", 2)
    f.createDimension("num_elem_var", 1)
    tw = f.createVariable("time_whole", "d", ("time_step",))
    names = f.createVariable("name_nod_var", "c", ("num_nod_var", "len_name"))
    names[:] = np.zeros((2, 33), "S1"); names[0, :11] = list("temperature"); names[1, :8] = list("pressure")
    en = f.createVariable("name_elem_var", "c", ("num_elem_var", "len_name"))
    en[:] = np.zeros((1, 33), "S1"); en[0, :6] = list("stress")
    v1 = f.createVariable("vals_nod_var1", "d", ("time_step", "num_nodes"))
    v2 = f.createVariable("vals_nod_var2", "d", ("time_step", "num_nodes"))
    e1 = f.createVariable("vals_elem_var1eb1", "d", ("time_step", "num_el_in_blk1"))
    e2 = f.createVariable("vals_elem_var1eb2", "d", ("time_step", "num_el_in_blk2"))
    for t in range(T):
        tw[t] = 10.0 * t
        v1[t] = temp(t); v2[t] = np.full(N, t)
        e1[t] = [1, 2, 3]; e2[t] = [4, 5]
    f.close()
    s = read_snapshots(p)
    assert [t for t, _ in s] == [0.0, 10.0, 20.0, 30.0]
    assert set(s[0][1]) == {"temperature", "pressure"}
    assert np.allclose(s[2][1]["temperature"], temp(2))
    s = read_snapshots(p, options={"exodus": {"element_variables": True}})
    assert np.allclose(s[1][1]["stress"], [1, 2, 3, 4, 5])
    assert "pressure" in s[1][1]


def test_xdmf_heavy_data_must_stay_inside_root(tmp_path):
    h5py = pytest.importorskip("h5py")
    with h5py.File(tmp_path / "outside.h5", "w") as f:
        f["a"] = np.arange(5.0)
    (tmp_path / "sub").mkdir()
    p = tmp_path / "sub" / "x.xdmf"
    p.write_text("""<Xdmf><Domain><Grid><Attribute Name="a"><DataItem Format="HDF" Dimensions="5">../outside.h5:/a</DataItem>
</Attribute></Grid></Domain></Xdmf>""")
    with pytest.raises(ReaderError):
        read_snapshots(p)
    assert np.allclose(read_snapshots(p, root=tmp_path)[0][1]["a"], np.arange(5.0))


def test_xml_entity_expansion_refused(tmp_path):
    ents = "".join(f'<!ENTITY e{i} "{("&e%d;" % (i - 1)) * 10 if i else "x" * 10}">' for i in range(6))
    p = tmp_path / "bomb.xdmf"
    p.write_text(f'<?xml version="1.0"?><!DOCTYPE d [{ents}]><Xdmf><Domain><Grid Name="&e5;"/></Domain></Xdmf>')
    with pytest.raises(Exception):
        read_snapshots(p)
