from __future__ import absolute_import, division, print_function

import pytest
import numpy as np

import astropy.units as u
from astropy.io import fits
from astropy.wcs import WCS
from astropy.table import Table, Column

from astropy.stats.funcs import gaussian_fwhm_to_sigma
from photutils.datasets import make_gaussian_sources_image

import numpy.testing as npt


# from nikamap.nikamap import NikaMap, jackknifey

# import nikamap as nm
# data_path = op.join(nm.__path__[0], 'data')

from ..analysis import jackknife, bootstrap


@pytest.fixture(scope='session')
def generate_nikamaps(tmpdir_factory):
    # Generate several maps with sources and noise... only one band...

    tmpdir = tmpdir_factory.mktemp("nm_maps")

    shape = (60, 60)
    pixsize = 1/3 * u.deg
    noise_level = 1 * u.Jy / u.beam
    nmaps = 10
    nsources = 5
    fwhm = 1*u.deg

    wcs = WCS()
    wcs.wcs.crpix = np.asarray(shape)/2-0.5  # Center of pixel
    wcs.wcs.cdelt = np.asarray([-1, 1])*pixsize
    wcs.wcs.ctype = ('RA---TAN', 'DEC--TAN')

    # Fake sources for all maps
    np.random.seed(3)
    sources = Table(masked=True)
    sources['amplitude'] = np.random.uniform(1, 10, size=nsources) * u.Jy
    sources['x_mean'] = np.random.uniform(1/4, 3/4, size=nsources) * shape[1]
    sources['y_mean'] = np.random.uniform(1/4, 3/4, size=nsources) * shape[0]

    beam_std_pix = (fwhm / pixsize).decompose().value * gaussian_fwhm_to_sigma
    sources['x_stddev'] = np.ones(nsources) * beam_std_pix
    sources['y_stddev'] = np.ones(nsources) * beam_std_pix
    sources['theta'] = np.zeros(nsources)

    data_sources = make_gaussian_sources_image(shape, sources) * u.Jy / u.beam

    a, d = wcs.wcs_pix2world(sources['x_mean'], sources['y_mean'], 0)
    sources.add_columns([Column(a * u.deg, name='ra'),
                         Column(d * u.deg, name='dec')])
    sources.remove_columns(['x_mean', 'y_mean', 'x_stddev', 'y_stddev', 'theta'])
    sources.sort('amplitude')
    sources.reverse()
    sources.add_column(Column(np.arange(len(sources)), name='ID'), 0)

    xx, yy = np.indices(shape)
    mask = np.sqrt((xx-shape[1]/2)**2 + (yy-shape[0]/2)**2) >= shape[0]/2

    primary_header = fits.header.Header()
    primary_header['f_sampli'] = 10., 'Fake the f_sampli keyword'
    primary_header['FWHM_260'] = fwhm.to(u.arcsec).value, '[arcsec] Fake the FWHM_260 keyword'
    primary_header['FWHM_150'] = fwhm.to(u.arcsec).value, '[arcsec] Fake the FWHM_150 keyword'

    primary_header['nsources'] = nsources, 'Number of fake sources'
    primary_header['pixsize'] = pixsize.to(u.deg).value, '[deg] pixel size'
    primary_header['nmaps'] = nmaps, 'number of maps produced'
    primary_header['shape0'] = shape[0], '[0] of map shape'
    primary_header['shape1'] = shape[1], '[1] of map shape'
    primary_header['noise'] = noise_level.to(u.Jy/u.beam).value, '[Jy/beam] noise level per map'

    primary = fits.hdu.PrimaryHDU(header=primary_header)

    filenames = []

    for i_map in range(nmaps):

        filename = str(tmpdir.join('map_{}.fits'.format(i_map)))

        hits = np.ones(shape=shape, dtype=np.float)
        uncertainty = np.ones(shape=shape, dtype=np.float) * noise_level
        data = np.random.normal(loc=0, scale=1, size=shape) * uncertainty

        data += data_sources
        data[mask] = 0
        hits[mask] = 0
        uncertainty[mask] = 0

        header = wcs.to_header()
        header['UNIT'] = "Jy / beam", 'Fake Unit'

        hdus = fits.hdu.HDUList(hdus=[primary])

        for band in ['1mm', '2mm']:
            hdus.append(fits.hdu.ImageHDU(data.value, header=header, name='Brightness_{}'.format(band)))
            hdus.append(fits.hdu.ImageHDU(uncertainty.value, header=header, name='Stddev_{}'.format(band)))
            hdus.append(fits.hdu.ImageHDU(hits, header=header, name='Nhits_{}'.format(band)))
            hdus.append(fits.hdu.BinTableHDU(sources, name="fake_sources"))

        hdus.writeto(filename, overwrite=True)

        filenames.append(filename)

    return filenames


def test_jackknife_average(generate_nikamaps):
    filenames = generate_nikamaps

    primary_header = fits.getheader(filenames[0], 0)
    weighted_noise = primary_header['NOISE'] / np.sqrt(primary_header['NMAPS'])

    # Weighted average
    data = next(jackknife(filenames, n=None))
    assert np.all(data.uncertainty.array[~data.mask] == weighted_noise)


def test_jackknife_one(generate_nikamaps):
    filenames = generate_nikamaps

    primary_header = fits.getheader(filenames[0], 0)
    weighted_noise = primary_header['NOISE'] / np.sqrt(primary_header['NMAPS'])

    # Produce one jackknife
    data = next(jackknife(filenames, n=1))
    assert np.all(data.uncertainty.array[~data.mask] == weighted_noise)
    npt.assert_allclose(np.std(data.data[~data.mask]), weighted_noise, rtol=1e-2)


def test_jackknife_iterator(generate_nikamaps):
    filenames = generate_nikamaps

    iterator = jackknife(filenames, n=10)
    assert len(list(iterator)) == 10

    with pytest.raises(StopIteration):
        next(iterator)


def test_jackknife_odd(generate_nikamaps):
    filenames = generate_nikamaps

    # Odd number
    with pytest.warns(UserWarning):
        iterator = jackknife(filenames[1:], n=1)


def test_jackknife_absent(generate_nikamaps):
    filenames = generate_nikamaps

    # Non existent files
    with pytest.warns(UserWarning):
        iterator = jackknife([filenames[0], filenames[1], 'toto.fits'], n=1)

    # Non existent files
    with pytest.raises(AssertionError):
        iterator = jackknife([filenames[0]], n=1)

    # Non existent files
    with pytest.warns(UserWarning):
        with pytest.raises(AssertionError):
            iterator = jackknife([filenames[0], 'toto.fits'], n=1)


def test_bootstrap(generate_nikamaps):
    filenames = generate_nikamaps

    primary_header = fits.getheader(filenames[0], 0)
    weighted_noise = primary_header['NOISE'] / np.sqrt(primary_header['NMAPS'])

    np.random.seed(0)
    nm = bootstrap(filenames, n_bootstrap=len(filenames)*10)
    mean_std = np.mean(nm.uncertainty.array[~nm.mask])
    std_std = np.std(nm.uncertainty.array[~nm.mask])
    assert np.abs(weighted_noise - mean_std) < std_std

    # For some reason, it seems that the bootstrap std is biaised....
    # n_bootstraps = np.logspace(np.log10(10), np.log10(1000), 10).astype(np.int)
    # mean_std = []
    # std_std = []
    # for n_bootstrap in n_bootstraps:
    #     nm = bootstrap(filenames, n_bootstrap=n_bootstrap)
    #     mean_std.append(np.mean(nm.uncertainty.array[~nm.mask]))
    #     std_std.append(np.std(nm.uncertainty.array[~nm.mask]))
    #
    # # We are actually limited by the number of input maps here...
    # plt.errorbar(n_bootstraps, mean_std, std_std)
    # plt.axhline(weighted_noise)


def test_weigthed_bootstrap(generate_nikamaps):
    filenames = generate_nikamaps

    primary_header = fits.getheader(filenames[0], 0)
    weighted_noise = primary_header['NOISE'] / np.sqrt(primary_header['NMAPS'])

    # This is equivalent to the unweighted case as the weights are all the same
    np.random.seed(0)
    nm = bootstrap(filenames, n_bootstrap=len(filenames)*10, wmean=True)
    mean_std = np.mean(nm.uncertainty.array[~nm.mask])
    std_std = np.std(nm.uncertainty.array[~nm.mask])
    assert np.abs(weighted_noise - mean_std) < std_std