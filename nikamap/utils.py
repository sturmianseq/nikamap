from __future__ import absolute_import, division, print_function

import numpy as np
import warnings

from astropy.io import fits
from astropy import units as u
from astropy.wcs import WCS
from astropy.modeling import Parameter, Fittable2DModel
from astropy.stats.funcs import gaussian_fwhm_to_sigma
from astropy.nddata import StdDevUncertainty

Jy_beam = u.Jy / u.beam

__all__ = ['fake_data']


# Forking from astropy.convolution.kernels
def _round_up_to_odd_integer(value):
    i = int(np.ceil(value))  # TODO: int() call is only needed for six.PY2
    if i % 2 == 0:
        return i + 1
    else:
        return i


class CircularGaussianPSF(Fittable2DModel):
    r"""
    Circular Gaussian model, not integrated, un-normalized.

    Parameters
    ----------
    sigma : float
        Width of the Gaussian PSF.
    flux : float (default 1)
        Total integrated flux over the entire PSF
    x_0 : float (default 0)
        Position of the peak in x direction.
    y_0 : float (default 0)
        Position of the peak in y direction.

    """

    flux = Parameter(default=1)
    x_0 = Parameter(default=0)
    y_0 = Parameter(default=0)
    sigma = Parameter(default=1, fixed=True)

    _erf = None
    fit_deriv = None

    @property
    def bounding_box(self):
        halfwidth = 4 * self.sigma
        return ((int(self.y_0 - halfwidth), int(self.y_0 + halfwidth)),
                (int(self.x_0 - halfwidth), int(self.x_0 + halfwidth)))

    def __init__(self, sigma=sigma.default,
                 x_0=x_0.default, y_0=y_0.default, flux=flux.default,
                 **kwargs):
        if self._erf is None:
            from scipy.special import erf
            self.__class__._erf = erf

        super(CircularGaussianPSF, self).__init__(n_models=1, sigma=sigma,
                                                  x_0=x_0, y_0=y_0,
                                                  flux=flux, **kwargs)

    def evaluate(self, x, y, flux, x_0, y_0, sigma):
        """Model function Gaussian PSF model."""

        return flux * np.exp(-((x - x_0)**2 + (y - y_0)**2) / (2*sigma**2))


def fake_header(shape=(512, 512), beam_fwhm=12.5 * u.arcsec, pixsize=2 * u.arcsec):
    """Build fake header"""

    header = fits.Header()
    header['NAXIS'] = (2, 'Number of data axes')
    header['NAXIS1'] = (shape[1], '')
    header['NAXIS2'] = (shape[0], '')

    header['CTYPE1'] = ('RA---TAN', 'Coordinate Type')
    header['CTYPE2'] = ('DEC--TAN', 'Coordinate Type')
    header['EQUINOX'] = (2000, 'Equinox of Ref. Coord.')

    header['CRPIX1'] = (shape[1] / 2, 'Reference Pixel in X')
    header['CRPIX2'] = (shape[0] / 2, 'Reference Pixel in Y')

    header['CRVAL1'] = (189, 'R.A. (degrees) of reference pixel')
    header['CRVAL2'] = (62, 'Declination of reference pixel')

    header['CDELT1'] = (-pixsize.to(u.deg).value, 'Degrees / Pixel')
    header['CDELT2'] = (pixsize.to(u.deg).value, 'Degrees / Pixel')

    header['OBJECT'] = ('fake', 'Name of the object')
    header['BMAJ'] = (beam_fwhm.to(u.deg).value, '[deg],  Beam major axis')
    header['BMIN'] = (beam_fwhm.to(u.deg).value, '[deg],  Beam major axis')

    return header


def fake_data(shape=(512, 512), beam_fwhm=12.5 * u.arcsec, pixsize=2 * u.arcsec, NEFD=50e-3 * Jy_beam * u.s**0.5,
              nsources=32, grid=False, wobble=False, peak_flux=None, time_fwhm=1. / 5, jk_data=None, e_data=None):
    """Build fake dataset"""

    # To avoid import loops
    from .nikamap import NikaMap

    if jk_data is not None:
        # JK data, extract all...
        data = jk_data.data
        e_data = jk_data.uncertainty
        mask = jk_data.mask
        time = jk_data.time
        header = jk_data.wcs.to_header()
        shape = data.shape
    elif e_data is not None:
        # Only gave e_data
        mask = np.isnan(e_data)
        time = ((e_data / NEFD)**(-1. / 0.5)).to(u.h)
        e_data = e_data.to(Jy_beam).value

        data = np.random.normal(0, 1, size=shape) * e_data

    else:
        # Regular gaussian noise
        if time_fwhm is not None:
            # Time as a centered gaussian
            y_idx, x_idx = np.indices(shape, dtype=np.float)
            time = np.exp(-((x_idx - shape[1] / 2)**2 / (2 * (gaussian_fwhm_to_sigma * time_fwhm * shape[1])**2) +
                            (y_idx - shape[0] / 2)**2 / (2 * (gaussian_fwhm_to_sigma * time_fwhm * shape[0])**2))) * u.h
        else:
            # Time is uniform
            time = np.ones(shape) * u.h

        mask = time < 1 * u.s
        time[mask] = np.nan

        e_data = (NEFD * time**(-0.5)).to(Jy_beam).value

        # White noise plus source
        data = np.random.normal(0, 1, size=shape) * e_data

    header = fake_header(shape, beam_fwhm, pixsize)
    header['NEFD'] = (NEFD.to(Jy_beam * u.s**0.5).value,
                      '[Jy/beam sqrt(s)], NEFD')

    # min flux which should be recoverable at the center of the field at 3 sigma
    if peak_flux is None:
        peak_flux = 3 * (NEFD / np.sqrt(np.nanmax(time)) * u.beam).to(u.mJy)

    data = NikaMap(data, mask=mask, unit=Jy_beam, uncertainty=StdDevUncertainty(
        e_data), wcs=WCS(header), meta=header, time=time)

    if nsources:
        data.add_gaussian_sources(nsources=nsources, peak_flux=peak_flux, grid=grid, wobble=wobble)

    return data


def pos_uniform(nsources=1, shape=None, within=(0, 1), mask=None, dist_threshold=0, max_loop=10):
    """Generate x, y uniform position within a mask, with a minimum distance between them

    Notes
    -----
    depending on the distance threshold and the number of loop, the requested number of sources might not be returned
    """

    pos = np.array([[], []], dtype=np.float).T

    i_loop = 0
    while i_loop < max_loop and len(pos) < nsources:
        i_loop += 1

        # note that these are pixels 0-indexes
        pos = np.concatenate((pos, np.random.uniform(within[0], within[1], (nsources, 2)) * np.asarray(shape) - 0.5))

        # Filter sources inside the mask
        if mask is not None:
            pos_idx = np.floor(pos + 0.5).astype(int)
            inside = ~mask[pos_idx[:, 0], pos_idx[:, 1]]
            pos = pos[inside]

        # Removing too close sources
        dist_mask = np.ones(len(pos), dtype=np.bool)
        while not np.all(~dist_mask):
            # Computing pixel distances between all sources
            dist = np.sqrt(np.sum((pos.reshape(len(pos), 1, 2) - pos)**2, 2))

            # Filter 0 distances and find minima
            i = np.arange(len(pos))
            dist[i, i] = np.inf
            arg_min_dist = np.argmin(dist, 1)
            min_dist = dist[i, arg_min_dist]
            # This will mask pair of sources with dist < dist_threshold
            dist_mask = min_dist < dist_threshold

            # un-mask the second source
            for idx, arg_min in enumerate(arg_min_dist):
                if dist_mask[idx]:
                    dist_mask[arg_min] = False

            pos = pos[~dist_mask]

        pos = pos[0:nsources]

    if i_loop == max_loop and len(pos) < nsources:
        warnings.warn("Maximum of loops reached, only have {} positions".format(len(pos)), UserWarning)

    return pos[:, 1], pos[:, 0]


def pos_gridded(nsources=2**2, shape=None, within=(0, 1), mask=None, wobble=False, wobble_frac=1):
    """Generate x, y gridded position within a mask

    Parameters
    ----------
    wobble : boolean
        Add a random offset with fwhm = grid_step * wobble_frac

    Notes
    -----
    requested number of sources might not be returned"""

    sq_sources = int(np.sqrt(nsources))
    assert sq_sources**2 == nsources, 'nsources must be a squared number'
    assert nsources > 1, 'nsouces can not be 1'

    # square distribution with step margin on the side
    within_step = (within[1] - within[0]) / (sq_sources + 1)
    pos = np.indices([sq_sources] * 2, dtype=np.float) * within_step + within[0] + within_step

    if wobble:
        # With some wobbling if needed
        pos += np.random.normal(0, within_step * wobble_frac * gaussian_fwhm_to_sigma, pos.shape)

    pos = pos.reshape(2, nsources).T

    # wobbling can push sources outside the shape
    inside = np.sum((pos >= 0) & (pos <= 1), 1) == 2
    pos = pos[inside]

    pos = pos * np.asarray(shape) - 0.5

    if mask is not None:
        pos_idx = np.floor(pos + 0.5).astype(int)
        inside = ~mask[pos_idx[:, 0], pos_idx[:, 1]]
        pos = pos[inside]

    if len(pos) < nsources:
        warnings.warn("Only {} positions".format(len(pos)), UserWarning)

    return pos[:, 1], pos[:, 0]


def pos_list(nsources=1, shape=None, within=(0, 1), mask=None, x_mean=None, y_mean=None):
    """Return positions within a mask

    Notes
    -----
    requested number of sources might not be returned"""

    assert x_mean is not None and y_mean is not None, 'you must provide x_mean & y_mean'
    assert len(x_mean) == len(y_mean), 'x_mean and y_mean must have the same dimension'
    assert nsources <= len(x_mean), 'x_mean must contains at least {} sources'.format(nsources)

    pos = np.array([y_mean, x_mean]).T

    # within
    limits = shape * np.asarray(within)[:, np.newaxis]
    inside = np.sum((pos >= limits[0]) & (pos <= limits[1]-1), 1) == 2
    pos = pos[inside]

    if mask is not None:
        pos_idx = np.floor(pos + 0.5).astype(int)
        inside = ~mask[pos_idx[:, 0], pos_idx[:, 1]]
        pos = pos[inside]

    if len(pos) < nsources:
        warnings.warn("Only {} positions".format(len(pos)), UserWarning)

    return pos[:, 1], pos[:, 0]