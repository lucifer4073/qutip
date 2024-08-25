"""
Classes that describe environments of open quantum systems
"""

# Required for Sphinx to follow autodoc_type_aliases
from __future__ import annotations

__all__ = ['BosonicEnvironment',
           'DrudeLorentzEnvironment',
           'UnderDampedEnvironment',
           'OhmicEnvironment']

import abc
from time import time
from typing import Any, Callable
import warnings

import numpy as np
from numpy.typing import ArrayLike
from scipy.integrate import quad_vec
from scipy.interpolate import interp1d
from scipy.interpolate import InterpolatedUnivariateSpline

try:
    from mpmath import mp
    _mpmath_available = True
except ModuleNotFoundError:
    _mpmath_available = False

from ..utilities import n_thermal
from ..solver.heom.bofin_baths import (
    UnderDampedBath, DrudeLorentzBath, DrudeLorentzPadeBath, BosonicBath,
    BathExponent)
from .fit_utils import (_run_fit, _gen_summary,
                        _two_column_summary, aaa, filter_poles)


class BosonicEnvironment(abc.ABC):
    """
    The bosonic environment of an open quantum system. It is characterized by
    its spectral density or, equivalently, its power spectrum or its two-time
    auto-correlation functions.

    Use one of the functions `BosonicEnvironment.from_spectral_density`,
    `BosonicEnvironment.from_power_spectrum` or
    `BosonicEnvironment.from_correlation_function` to contruct an environment
    manually from one of these characteristic functions, or use a predefined
    sub-class such as the `DrudeLorentzEnvironment` or the
    `UnderdampedEnvironment`.
    """
    # TODO: proper links in docstring
    # TODO: properly document all our definitions of these functions in the users guide
    # especially how we handle negative frequencies and all that stuff

    def __init__(self, T: float = None, *, tag: Any = None):
        self.T = T
        self.tag = tag

    @abc.abstractmethod
    def spectral_density(self, w: float | ArrayLike):
        ...

    @abc.abstractmethod
    def correlation_function(self, t: float | ArrayLike, **kwargs):
        # Default implementation: calculate from SD by numerical integration
        def integrand(w, t):
            return self.spectral_density(w) / np.pi * (
                (2 * n_thermal(w, self.T) + 1) * np.cos(w * t)
                - 1j * np.sin(w * t)
            )

        result = quad_vec(lambda w: integrand(w, t), 0, np.inf, **kwargs)
        return result[0]

    @abc.abstractmethod
    def power_spectrum(self, w: float | ArrayLike, dt: float = 1e-5):
        # Default implementation: calculate from SD directly
        w = np.array(w, dtype=float)

        # at omega=0, typically SD is zero and n_thermal is undefined
        # set omega to small positive value to capture limit as omega -> 0
        w[w == 0.0] += 1e-6
        if self.T != 0:
            S = (2 * np.sign(w) * self.spectral_density(np.abs(w)) *
                 (n_thermal(w, self.T) + 1))
        else:
            S = 2 * np.heaviside(w, 0) * self.spectral_density(w)
        return S

    # TODO: I thought these temperatures can be `None`. In what cases is that ok?
    @classmethod
    def from_correlation_function(
        cls,
        T: float,
        C: Callable[[float], complex] | ArrayLike,
        tlist: ArrayLike = None,
        *,
        tag: Any = None
    ) -> BosonicEnvironment:
        """
        Constructs a bosonic environment with the provided correlation
        function and temperature

        Parameters
        ----------
        T : float
            Bath temperature.

        C: callable or :obj:`np.array`
            The correlation function

        tlist : :obj:`np.array` (optional)
            The times where the correlation function is sampled (if it is
            provided as an array)

        tag : optional, str, tuple or any other object
            An identifier (name) for this environment
        """
        return _BosonicEnvironment_fromCF(T, C, tlist, tag)

    @classmethod
    def from_power_spectrum(
        cls,
        T: float,
        S: Callable[[float], float] | ArrayLike,
        wlist: ArrayLike = None,
        *,
        tag: Any = None
    ) -> BosonicEnvironment:
        """
        Constructs a bosonic environment with the provided power spectrum
        and temperature

        Parameters
        ----------
        T : float
            Bath temperature.

        S: callable or :obj:`np.array.`
            The power spectrum

        wlist : :obj:`np.array` (optional)
            The frequencies where the power spectrum is sampled (if it is
            provided as an array)

        tag : optional, str, tuple or any other object
            An identifier (name) for this environment
        """
        return _BosonicEnvironment_fromPS(T, S, wlist, tag)

    @classmethod
    def from_spectral_density(
        cls,
        T: float,
        J: Callable[[float], float] | ArrayLike,
        wlist: ArrayLike = None,
        *,
        tag: Any = None
    ) -> BosonicEnvironment:
        """
        Constructs a bosonic environment with the provided spectral density
        and temperature

        Parameters
        ----------
        T : float
            Bath temperature.

        J : callable or :obj:`np.array`
            The spectral density

        wlist : :obj:`np.array` (optional)
            The frequencies where the spectral density is sampled (if it is
            provided as an array)

        tag : optional, str, tuple or any other object
            An identifier (name) for this environment
        """
        return _BosonicEnvironment_fromSD(T, J, wlist, tag)


class _BosonicEnvironment_fromCF(BosonicEnvironment):
    def __init__(self, T, C, tlist=None, tag=None):
        super().__init__(T, tag=tag)
        self._cf = _complex_interpolation(C, tlist, 'correlation function')

    def correlation_function(self, t, **kwargs):
        # TODO document that this does this weird thing (or remove)?
        result = np.zeros_like(t, dtype=complex)
        positive_mask = t > 0
        non_positive_mask = ~positive_mask

        result[positive_mask] = self._cf(t[positive_mask])
        result[non_positive_mask] = np.conj(
            self._cf(np.abs(t[non_positive_mask]))
        )
        return result

    def spectral_density(self, w):
        return self.power_spectrum(w) / (n_thermal(w, self.T) + 1) / 2

    def power_spectrum(self, w, dt=1e-5):
        negative = _fft(self.correlation_function, w[-1], dt)
        return negative(-w)


class _BosonicEnvironment_fromPS(BosonicEnvironment):
    def __init__(self, T, S, wlist=None, tag=None):
        super().__init__(T, tag=tag)
        self._ps = _real_interpolation(S, wlist, 'power spectrum')

    def correlation_function(self, t, **kwargs):
        def integrand(w, t):
            return self.spectral_density(w) / np.pi * (
                (2 * n_thermal(w, self.T) + 1) * np.cos(w * t)
                - 1j * np.sin(w * t)
            )

        result = quad_vec(lambda w: integrand(w, t), 0, np.inf, **kwargs)
        return result[0]

    def spectral_density(self, w):
        # TODO is this okay at w=0 or do we have to do something like in _fromSD?
        return self.power_spectrum(w) / (n_thermal(w, self.T) + 1) / 2

    def power_spectrum(self, w, dt=1e-5):
        return self._ps(w)


class _BosonicEnvironment_fromSD(BosonicEnvironment):
    def __init__(self, T, J, wlist=None, tag=None):
        super().__init__(T, tag=tag)
        self._sd = _real_interpolation(J, wlist, 'spectral density')

    def correlation_function(self, t, **kwargs):
        return super().correlation_function(t, **kwargs)

    def spectral_density(self, w):
        return self._sd(w)

    def power_spectrum(self, w, dt=1e-5):
        return super().power_spectrum(w, dt)


class DrudeLorentzEnvironment(BosonicEnvironment):
    """
    Describes a Drude-Lorentz bosonic environment with the following
    parameters:

    Parameters
    ----------
    T : float
        Bath temperature.

    lam : float
        Coupling strength.

    gamma : float
        Bath spectral density cutoff frequency.

    tag : optional, str, tuple or any other object
        An identifier (name) for this environment
    """

    def __init__(
        self, T: float, lam: float, gamma: float, *, tag: Any = None
    ):
        super().__init__(T, tag=tag)
        self.lam = lam
        self.gamma = gamma

    def spectral_density(self, w: float | ArrayLike):
        r"""
        Calculates the Drude-Lorentz spectral density, Eq. 15 in the BoFiN
        paper (DOI: 10.1103/PhysRevResearch.5.013181) given by

        .. math::

            J(\omega) = \frac{2 \lambda \gamma \omega}{\gamma^{2}+\omega^{2}}

        Parameters
        ----------
        w: float or array
            Energy of the mode.

        Returns
        -------
        The spectral density of the mode with energy w.
        """

        return 2 * self.lam * self.gamma * w / (self.gamma**2 + w**2)

    def correlation_function(
        self, t: float | ArrayLike, Nk: int = 15000, **kwargs
    ):
        """
        Here we determine the correlation function by summing a large number
        of exponents, as the numerical integration is noisy for this spectral
        density.

        Parameters
        ----------
        t : np.array or float
            The time at which to evaluate the correlation function
        Nk : int, default 15000
            The number of exponents to use
        """

        ck_real, vk_real, ck_imag, vk_imag = self._matsubara_params(Nk)

        def C(c, v):
            return np.sum([ck * np.exp(-np.array(vk * t))
                           for ck, vk in zip(c, v)], axis=0)
        return C(ck_real, vk_real) + 1j * C(ck_imag, vk_imag)

    def power_spectrum(self, w: float | ArrayLike, dt: float = 1e-5):
        return super().power_spectrum(w, dt)

    def _matsubara_params(self, Nk):
        """ Calculate the Matsubara coefficients and frequencies. """
        ck_real = [self.lam * self.gamma / np.tan(self.gamma / (2 * self.T))]
        ck_real.extend([
            (8 * self.lam * self.gamma * self.T * np.pi * k * self.T /
                ((2 * np.pi * k * self.T)**2 - self.gamma**2))
            for k in range(1, Nk + 1)
        ])
        vk_real = [self.gamma]
        vk_real.extend([2 * np.pi * k * self.T for k in range(1, Nk + 1)])

        ck_imag = [-self.lam * self.gamma]
        vk_imag = [self.gamma]

        return ck_real, vk_real, ck_imag, vk_imag

class UnderDampedEnvironment(BosonicEnvironment):
    """
    Describes an underdamped environment with the following parameters:

    Parameters
    ----------
    T : float
        Bath temperature.

    lam : float
        Coupling strength.

    gamma : float
        Bath spectral density cutoff frequency.

    w0 : float
        Bath spectral density resonance frequency.

    tag : optional, str, tuple or any other object
        An identifier (name) for this environment
    """

    def __init__(
        self, T: float, lam: float, gamma: float, w0: float, *, tag=None
    ):
        super().__init__(T, tag=tag)
        self.lam = lam
        self.gamma = gamma
        self.w0 = w0

    def spectral_density(self, w: float | ArrayLike):
        r"""
        Calculates the underdamped spectral density, see Eq. 16 in the BoFiN
        paper (DOI: 10.1103/PhysRevResearch.5.013181)

        .. math::
            J(\omega) = \frac{\lambda^{2} \Gamma \omega}{(\omega_{c}^{2}-
            \omega^{2})^{2}+ \Gamma^{2} \omega^{2}}

        Parameters
        ----------
        w: float or array
            Energy of the mode.

        Returns
        -------
        The spectral density of the mode with energy w.
        """

        return self.lam**2 * self.gamma * w / ((w**2 - self.w0**2)**2
                                               + (self.gamma*w)**2)

    def correlation_function(self, t: float | ArrayLike, **kwargs):
        return super().correlation_function(t, **kwargs)

    def power_spectrum(self, w: float | ArrayLike, dt: float = 1e-5):
        return super().power_spectrum(w, dt)

    def _matsubara_params(self, Nk):
        """ Calculate the Matsubara coefficients and frequencies. """
        beta = 1 / self.T
        Om = np.sqrt(self.w0**2 - (self.gamma / 2)**2)
        Gamma = self.gamma / 2

        ck_real = ([
            (self.lam**2 / (4 * Om))
            * (1 / np.tanh(beta * (Om + 1j * Gamma) / 2)),
            (self.lam**2 / (4 * Om))
            * (1 / np.tanh(beta * (Om - 1j * Gamma) / 2)),
        ])

        ck_real.extend([
            (-2 * self.lam**2 * self.gamma / beta) * (2 * np.pi * k / beta)
            / (
                ((Om + 1j * Gamma)**2 + (2 * np.pi * k / beta)**2)
                * ((Om - 1j * Gamma)**2 + (2 * np.pi * k / beta)**2)
            )
            for k in range(1, Nk + 1)
        ])

        vk_real = [-1j * Om + Gamma, 1j * Om + Gamma]
        vk_real.extend([
            2 * np.pi * k * self.T
            for k in range(1, Nk + 1)
        ])

        ck_imag = [
            1j * self.lam**2 / (4 * Om),
            -1j * self.lam**2 / (4 * Om),
        ]

        vk_imag = [-1j * Om + Gamma, 1j * Om + Gamma]

        return ck_real, vk_real, ck_imag, vk_imag


class OhmicEnvironment(BosonicEnvironment):
    """
    Describes an Ohmic environment. This class requires the `mpmath` module
    to be installed.

    Parameters
    ----------
    T : float
        Temperature of the bath.

    alpha : float
        Coupling strength.

    wc : float
        Cutoff parameter.

    s : float
        Power of omega in the spectral density.

    tag : optional, str, tuple or any other object
        An identifier (name) for this environment
    """

    def __init__(
        self, T: float, alpha: float, wc: float, s: float, *, tag: Any = None
    ):
        super().__init__(T, tag=tag)
        self.alpha = alpha
        self.wc = wc
        self.s = s

        if _mpmath_available is False:
            warnings.warn(
                "The mpmath module is required for some operations on "
                "Ohmic environments, but it is not available.")

    def spectral_density(self, w: float | ArrayLike):
        r"""
        Calculates the spectral density of an Ohmic Bath (sub and super-Ohmic
        included according to the choice of s).

        .. math::
            J(w) = \alpha \frac{w^{s}}{w_{c}^{1-s}} e^{-\frac{|w|}{w_{c}}}

        Parameters
        ----------
        w : float or :obj:`np.array`
            Energy of the mode.

        Returns
        -------
        The spectral density of the mode with energy w.
        """

        return (
            self.alpha * w ** self.s
            / (self.wc ** (1 - self.s))
            * np.exp(-np.abs(w) / self.wc)
        )

    def correlation_function(self, t: float | ArrayLike, **kwargs):
        r"""
        Calculates the correlation function of an Ohmic bath
        (sub and super-Ohmic included according to the choice of s).

        .. math::
            C(t)= \frac{1}{\pi} \alpha w_{c}^{1-s} \beta^{-(s+1)} \Gamma(s+1)
            \left[ \zeta\left(s+1,\frac{1+\beta w_{c} -i w_{c} t}{\beta w_{c}}
            \right) +\zeta\left(s+1,\frac{1+ i w_{c} t}{\beta w_{c}}\right)
            \right]

        where :math:`\Gamma` is the gamma function, and :math:`\zeta` the
        Riemann zeta function

        Parameters
        ----------
        t : float or :obj:`np.array`
            time.

        Returns
        -------
        The correlation function at time t.
        """

        if self.T != 0:
            corr = (self.alpha * self.wc ** (1 - self.s) / np.pi
                    * mp.gamma(self.s + 1) / self.T ** (-(self.s + 1)))
            z1_u = ((1 + self.wc / self.T - 1j * self.wc * t)
                    / (self.wc / self.T))
            z2_u = (1 + 1j * self.wc * t) / (self.wc / self.T)
            return np.array(
                [corr * (mp.zeta(self.s + 1, u1) + mp.zeta(self.s + 1, u2))
                 for u1, u2 in zip(z1_u, z2_u)],
                dtype=np.cdouble
            )
        else:
            corr = (self.alpha * self.wc ** (self.s+1) / np.pi
                    * mp.gamma(self.s + 1)
                    * (1 + 1j * self.wc * t) ** (-(self.s + 1)))
            return np.array(corr, dtype=np.cdouble)

    def power_spectrum(self, w: float | ArrayLike, dt: float = 1e-5):
        return super().power_spectrum(w, dt)






# --- utility functions ---

def _real_interpolation(fun, xlist, name):
    if callable(fun):
        return fun
    else:
        if xlist is None or len(xlist) != len(fun):
            raise ValueError("A list of x-values with the same length must be "
                             f"provided for the discretized function ({name})")
        return interp1d(
            xlist, fun, kind='cubic', fill_value='extrapolate'
        )

def _complex_interpolation(fun, xlist, name):
    if callable(fun):
        return fun
    else:
        real_interp = _real_interpolation(np.real(fun), xlist, name)
        imag_interp = _real_interpolation(np.imag(fun), xlist, name)
        return lambda x: real_interp(x) + 1j * imag_interp(x)

def _fft(fun, t0=10, dt=1e-5):
    """
    Calculates the Fast Fourier transform of the given function. This
    is an alternative to numerical integration which is often noisy in the 
    settings we are interested on

    Parameters
    ----------
    t0: float or obj:`np.array.`
        Range to use for the fast fourier transform, the range is [-t0,t0].
    dt: float
        The timestep to be used.

    Returns
    -------
    TThe fourier transform of the correlation function
    """
    t = np.arange(-t0, t0, dt)
    # Define function
    f = fun(t)

    # Compute Fourier transform by numpy's FFT function
    g = np.fft.fft(f)
    # frequency normalization factor is 2*np.pi/dt
    w = np.fft.fftfreq(f.size)*2*np.pi/dt
    # In order to get a discretisation of the continuous Fourier transform
    # we need to multiply g by a phase factor
    g *= dt*2.5*np.exp(-1j*w*t0)/(np.sqrt(2*np.pi))
    sorted_indices = np.argsort(w)
    zz = interp1d(w[sorted_indices], g[sorted_indices])
    return zz





# --- old code ---


class ExponentialBosonicBath(BosonicBath):
    """
    Hiden class that constructs a bosonic reservoir, from the coefficients and
    exponents

    Parameters
    ----------
    Q : Qobj
        The coupling operator for the bath.

    ck_real : list of complex
        The coefficients of the expansion terms for the real part of the
        correlation function. The corresponding frequencies are passed as
        vk_real.

    vk_real : list of complex
        The frequencies (exponents) of the expansion terms for the real part of
        the correlation function. The corresponding ceofficients are passed as
        ck_real.

    ck_imag : list of complex
        The coefficients of the expansion terms in the imaginary part of the
        correlation function. The corresponding frequencies are passed as
        vk_imag.

    vk_imag : list of complex
        The frequencies (exponents) of the expansion terms for the imaginary
        part of the correlation function. The corresponding ceofficients are
        passed as ck_imag.

    combine : bool, default True
        Whether to combine exponents with the same frequency (and coupling
        operator). See :meth:`combine` for details.

    tag : optional, str, tuple or any other object
        A label for the bath exponents (for example, the name of the
        bath). It defaults to None but can be set to help identify which
        bath an exponent is from.
    T: optional, float
        The temperature of the bath.
    """

    def __init__(self, Q, ck_real, vk_real, ck_imag, vk_imag, combine=True,
                 tag=None, T=None):
        super().__init__(Q, ck_real, vk_real, ck_imag, vk_imag, combine,
                         tag, T)

    def _bose_einstein(self, w):
        """
        Calculates the bose einstein distribution for the
        temperature of the bath.

        Parameters
        ----------
        w: float or obj:`np.array.`
            Energy of the mode.

        Returns
        -------
        The population of the mode with energy w.
        """

        if self.T is None:
            raise ValueError(
                "Bath temperature must be specified for this operation")
        if self.T == 0:
            return np.zeros_like(w)

        w = np.array(w, dtype=float)
        result = np.zeros_like(w)
        non_zero = w != 0
        result[non_zero] = 1 / (np.exp(w[non_zero] / self.T) - 1)
        return result

    def correlation_function_approx(self, t):
        """
        Computes the correlation function from the exponents. This is the
        approximation for the correlation function that is used in the HEOM
        construction.

        Parameters
        ----------
        t: float or obj:`np.array.`
            time to compute correlations.

        Returns
        -------
        The correlation function of the bath at time t.
        """

        corr = np.zeros_like(t, dtype=complex)
        for exp in self.exponents:
            if (
                exp.type == BathExponent.types['R'] or
                exp.type == BathExponent.types['RI']
            ):
                corr += exp.ck * np.exp(-exp.vk * t)
            if exp.type == BathExponent.types['I']:
                corr += 1j*exp.ck * np.exp(-exp.vk * t)
            if exp.type == BathExponent.types['RI']:
                corr += 1j*exp.ck2 * np.exp(-exp.vk * t)
        return corr

    def power_spectrum_approx(self, w):
        """
        Calculates the power spectrum from the exponents
        of the bosonic bath.

        Parameters
        ----------
        w: float or obj:`np.array.`
            Energy of the mode.

        Returns
        -------
        The power spectrum of the mode with energy w.
        """

        S = np.zeros_like(w, dtype=float)
        for exp in self.exponents:
            if (
                exp.type == BathExponent.types['R'] or
                exp.type == BathExponent.types['RI']
            ):
                coeff = exp.ck
            if exp.type == BathExponent.types['I']:
                coeff = 1j * exp.ck
            if exp.type == BathExponent.types['RI']:
                coeff += 1j * exp.ck2

            S += 2 * np.real((coeff) / (exp.vk - 1j*w))

        return S

    def spectral_density_approx(self, w):
        """
        Calculates the spectral density from the exponents
        of the bosonic bath.

        Parameters
        ----------
        w: float or obj:`np.array.`
            Energy of the mode.

        Returns
        -------
        The spectral density of the mode with energy w.
        """
        J = self.power_spectrum_approx(w) / (self._bose_einstein(w) + 1) / 2
        return J


class ApproximatedBosonicBath(ExponentialBosonicBath):
    """
    This class allows to construct a reservoir from the correlation function,
    power spectrum or spectral density.
    """
    @classmethod
    def from_sd(self, bath, N, Nk, x, Q):
        """
        Generates a reservoir from the spectral density

        Parameters
        ----------
        bath: obj:`BosonicReservoir`
            The reservoir we want to approximate
        N: int
            The number of modes to use for the fit
        Nk: int
            The number of exponents to use in each mode
        x: obj:`np.ndarray`
            The range on which to perform the fit
        Q: obj:`Qobj`
            The coupling operator to the bath

        Returns
        -------
        A bosonic reservoir
        """
        cls = SpectralFitter(bath.T, Q, x, bath.spectral_density)
        cls.get_fit(N=N, Nk=Nk)
        return cls

    @classmethod
    def from_ps(self, bath, x, tol, max_exponents, Q):
        """
        Generates a reservoir from the power spectrum

        Parameters
        ----------
        bath: obj:`BosonicReservoir`
            The reservoir we want to approximate
        tol: float
            The desired error tolerance
        max_exponents: int
            The maximum number of exponents allowed
        x: obj:`np.ndarray`
            The range on which to perform the fit
        Q: obj:`Qobj`
            The coupling operator to the bath

        Returns
        -------
        A bosonic reservoir
        """
        r, pol, res, zer, _ = aaa(bath.power_spectrum, x,
                                  tol=tol,
                                  max_iter=max_exponents*2)
        new_pols, new_res = filter_poles(pol, res)
        ckAR, ckAI = np.real(-1j*new_res), np.imag(-1j*new_res)
        vkAR, vkAI = np.real(1j*new_pols), np.imag(1j*new_pols)
        cls = ExponentialBosonicBath(
            Q=Q, ck_real=ckAR, vk_real=vkAR + 1j * vkAI, ck_imag=ckAI,
            vk_imag=vkAR + 1j * vkAI, T=bath.T)
        return cls

    @classmethod
    def from_cf(self, Q, x, bath, Nr, Ni, full_ansatz=False):
        """
        Generates a reservoir from the correlation function

        Parameters
        ----------
        bath: obj:`BosonicReservoir`
            The reservoir we want to approximate
        Nr: int
            The number of modes to use for the fit of the real part 
        Ni: int
            The number of modes to use for the fit of the real part 
        x: obj:`np.ndarray`
            The range on which to perform the fit
        Q: obj:`Qobj`
            The coupling operator to the bath
        full_ansatz: bool
            Whether to use a fit of the imaginary and real parts that is 
            complex
        Returns
        -------
        A bosonic reservoir
        """
        cls = CorrelationFitter(Q, bath.T, x, bath.correlation_function)
        cls.get_fit(
            Nr=Nr,
            Ni=Ni,
            full_ansatz=full_ansatz)
        return cls

    @classmethod
    def from_pade_OD(self, Q, T, lam, gamma, Nk, combine=True, tag=None):

        eta_p, gamma_p = self._corr(lam=lam, gamma=gamma, T=T, Nk=Nk)

        ck_real = [np.real(eta) for eta in eta_p]
        vk_real = [gam for gam in gamma_p]

        ck_imag = [np.imag(eta_p[0])]
        vk_imag = [gamma_p[0]]

        cls = _drudepade(
            Q, ck_real, vk_real, ck_imag, vk_imag,
            combine=combine, tag=tag, T=T
        )
        return cls

    @classmethod
    def from_matsubara_UD(
            self, Q, T, lam, gamma, w0, Nk, combine=True, tag=None):

        return _underdamped(
            Q=Q, T=T, lam=lam, gamma=gamma, w0=w0, Nk=Nk,
            combine=combine, tag=tag)

    @classmethod
    def from_matsubara_OD(self, lam, gamma, Nk, Q, T, combine=True, tag=None):
        return _drude(
            lam=lam, gamma=gamma, T=T, Q=Q, Nk=Nk, combine=combine, tag=tag)


class _drudepade(DrudeLorentzPadeBath, ExponentialBosonicBath):
    """
    Hidden class to have DrudeLorentzPadeBath inherit from 
    ExponentialBosonicBath
    """
    def __init__(
            self, Q, ck_real, vk_real, ck_imag, vk_imag, T, combine=True,
            tag=None):
        super().__init__(Q, ck_real, vk_real, ck_imag, vk_imag,
                         combine=combine, tag=tag, T=T)


class _drude(DrudeLorentzBath, ExponentialBosonicBath):
    """
    Hidden class to have DrudeLorentzBath inherit from
    ExponentialBosonicBath
    """
    def __init__(self, Q, lam, gamma, T, Nk, combine=True, tag=None):
        super().__init__(Q=Q, lam=lam, gamma=gamma, T=T, Nk=Nk,
                         combine=combine, tag=tag)


class _underdamped(UnderDampedBath, ExponentialBosonicBath):
    """
    Hidden class to have UnderDampedBath inherit from
    ExponentialBosonicBath
    """
    def __init__(self, Q, lam, gamma, w0, T, Nk, combine=True, tag=None):
        super().__init__(Q=Q, lam=lam, gamma=gamma, w0=w0, T=T, Nk=Nk,
                         combine=combine, tag=tag)


class SpectralFitter(ExponentialBosonicBath):
    """
    A helper class for constructing a Bosonic bath from a fit of the spectral
    density with a sum of underdamped modes.

    Parameters
    ----------
    Q : :obj:`.Qobj`
        Operator describing the coupling between system and bath.

    T : float
        Bath temperature.

    w : :obj:`np.array.`
        The range on which to perform the fit, it is recommended that it covers
        at least twice the cutoff frequency of the desired spectral density.

    J : :obj:`np.array.` or callable
        The spectral density to be fitted as an array or function.
    """

    def __init__(self, T, Q, w, J):
        self.Q = Q
        self.T = T
        self.fitinfo = None
        self.set_spectral_density(w, J)

    def set_spectral_density(self, w, J):
        """
        Sets the spectral density to be fitted. It may be provided either as an
        array of function values or as a python function. For internal reasons,
        it will then be interpolated or discretized as necessary.
        """

        if callable(J):
            self._w = w
            self._J_array = J(w)
            self._J_fun = J
        else:
            self._w = w
            self._J_array = J
            self._J_fun = InterpolatedUnivariateSpline(w, J)

    @classmethod
    def _meier_tannor_SD(cls, w, a, b, c):
        r"""
        Underdamped spectral density used for fitting in Meier-Tannor form
        (see Eq. 38 in the BoFiN paper, DOI: 10.1103/PhysRevResearch.5.013181)
        or the get_fit method.

        Parameters
        ----------
        w : :obj:`np.array.`
            The frequency of the spectral density
        a : :obj:`np.array.`
            Array of coupling constants ($\alpha_i^2$)
        b : :obj:`np.array.`
            Array of cutoff parameters ($\Gamma'_i$)
        c : :obj:`np.array.`
            Array of resonant frequencies ($\Omega_i$)
        """

        return sum((2 * ai * bi * w
                    / ((w + ci) ** 2 + bi ** 2)
                    / ((w - ci) ** 2 + bi ** 2))
                   for ai, bi, ci in zip(a, b, c))

    def get_fit(
        self,
        N=None,
        Nk=1,
        final_rmse=5e-6,
        lower=None,
        upper=None,
        sigma=None,
        guesses=None,
    ):
        r"""
        Provides a fit to the spectral density with N underdamped oscillator
        baths. N can be determined automatically based on reducing the
        normalized root mean squared error below a certain threshold.

        Parameters
        ----------
        N : optional, int
            Number of underdamped oscillators to use.
            If set to None, it is determined automatically.
        Nk : optional, int
            Number of exponential terms used to approximate the bath
            correlation functions, defaults to 1. To approximate the
            correlation function the number of exponents grow as the
            Desired normalized root mean squared error. Defaults to
            :math:`5\times10^{-6}`. Only used if N is set to None.
            Desired normalized root mean squared error. Defaults to
            Lower bounds on the parameters for the fit. A list of size 3,
            containing the lower bounds for :math:`a_i` (coupling constants),
            :math:`b_i` (cutoff frequencies) and :math:`c_i`
            (resonant frequencies) in the following fit function:

            .. math::
                J(\omega) = \sum_{i=1}^{k} \frac{2 a_{i} b_{i} \omega
                }{\left(\left( \omega + c_{i}\right)^{2} + b_{i}^{2}\right)
                \left(\left( \omega - c_{i}\right)^{2} + b_{i}^{2} \right)}

            The lower bounds are considered to be the same for all N modes.
            For example,

            lower=[0,-1,2]

            would bound the coupling to be bigger than 0, the cutoff frequency
            to be higher than 1, and the central frequency to be bigger than 2

        upper : list
            Upper bounds on the parameters for the fit, the structure is the
            same as the lower keyword.
        sigma : float
            Uncertainty in the data considered for the fit, all data points are
            considered to have the same uncertainty.
        guesses : list
            Initial guesses for the parameters. Same structure as lower and
            upper.

        Note: If one of lower, upper, sigma, guesses is None, all are discarded

        Returns
        -------
        1. A Bosonic Bath created with the fit parameters for the original
          spectral density function (that was provided or interpolated)
        2. A dictionary containing the following information about the fit:
            * fit_time:
                The time the fit took in seconds.
            * rsme:
                Normalized mean squared error obtained in the fit.
            * N:
                The number of terms used for the fit.
            * params:
                The fitted parameters (3N parameters), it contains three lists
                one for each parameter, each list containing N terms.
            * Nk:
                The number of exponents used to construct the bosonic bath.
            * summary:
                A string that summarizes the information of the fit.
        """

        start = time()
        rmse, params = _run_fit(
            SpectralFitter._meier_tannor_SD, self._J_array, self._w,
            final_rmse, default_guess_scenario="Spectral Density", N=N,
            sigma=sigma, guesses=guesses, lower=lower, upper=upper)
        end = time()

        fit_time = end - start
        spec_n = len(params[0])
        self._generate_bath(params, Nk)
        summary = _gen_summary(
            fit_time, rmse, N, "The Spectral Density", params)
        fitInfo = {
            "fit_time": fit_time, "rmse": rmse, "N": spec_n, "params": params,
            "Nk": Nk, "summary": summary}
        self.fitinfo = fitInfo

    def _generate_bath(self, params, Nk):
        """
        Obtains the bath exponents from the list of fit parameters. Some
        transformations are done, to reverse the ones in the UnderDampedBath.
        They are done to change the spectral density from eq. 38 to eq. 16
        of the BoFiN paper and vice-versa.

        Parameters
        ----------
        params: list
            The parameters obtained from the fit.

        Returns
        -------
            A Bosonic Bath created with the fit parameters.
        """

        lam, gamma, w0 = params
        w0 = np.array(
            [
                np.sqrt((w0[i] + 0j) ** 2 + (gamma[i] + 0j / 2) ** 2)
                for i in range(len(w0))
            ]
        )
        lam = np.sqrt(
            lam + 0j
        )
        # both w0, and lam modifications are needed to input the
        # right value of the fit into the Underdamped bath
        ckAR = []
        vkAR = []
        ckAI = []
        vkAI = []

        for lamt, Gamma, Om in zip(lam, gamma, w0):
            coeffs = UnderDampedBath._matsubara_params(
                lamt, 2 * Gamma, Om + 0j, self.T, Nk)
            ckAR.extend(coeffs[0])
            vkAR.extend(coeffs[1])
            ckAI.extend(coeffs[2])
            vkAI.extend(coeffs[3])

        super().__init__(self.Q, ckAR, vkAR, ckAI, vkAI, T=self.T)


class CorrelationFitter(ExponentialBosonicBath):
    """
    A helper class for constructing a Bosonic bath from a fit of the
    correlation function with exponential terms.

    Parameters
    ----------
    Q : :obj:`.Qobj`
        Operator describing the coupling between system and bath.
    T : float
        Temperature of the bath.
    t : :obj:`np.array.`
        The range which to perform the fit.
    C : :obj:`np.array.` or callable
        The correlation function to be fitted as an array or function.
    """

    def __init__(self, Q, T, t, C):
        self.Q = Q
        self.T = T
        self.fitinfo = None
        self.set_correlation_function(t, C)

    def set_correlation_function(self, t, C):
        """
        This function creates a discretized version of the correlation function
        if the correlation function is provided, and a function if
        an array is provided.

        The array is needed to run the least squares algorithm, while the
        the function is used to assign a correlation function to the bosonic
        bath object.
        """
        if callable(C):
            self._t = t
            self._C_array = C(t)
            self._C_fun = C
        else:
            self._t = t
            self._C_array = C
            _C_fun_r = InterpolatedUnivariateSpline(t, np.real(C))
            _C_fun_i = InterpolatedUnivariateSpline(t, np.imag(C))
            self._C_fun = lambda t: _C_fun_r(t) + 1j * _C_fun_i(t)

    def _corr_approx(self, t, a, b, c, d=0):
        r"""
        This is the form of the correlation function to be used for fitting.

        Parameters
        ----------
        t : :obj:`np.array.` or float
            The times at which to evaluates the correlation function.
        a : list or :obj:`np.array.`
            A list describing the  real part amplitude of the correlation
            approximation.
        b : list or :obj:`np.array.`
            A list describing the decay of the correlation approximation.
        c : list or :obj:`np.array.`
            A list describing the oscillations of the correlation
            approximation.
        d:  A list describing the imaginary part amplitude of the correlation
            approximation, only used if the user selects if the full_ansatz
            flag from get_fit is True.
        """

        a = np.array(a)
        b = np.array(b)
        c = np.array(c)
        d = np.array(d)
        if (d == 0).all():
            d = np.zeros(a.shape)

        return np.sum(
            (a[:, None]+1j*d[:, None]) * np.exp(b[:, None] * t[None, :]) *
            np.exp(1j*c[:, None] * t[None, :]),
            axis=0,
        )

    def get_fit(
        self,
        Nr=None,
        Ni=None,
        final_rmse=2e-5,
        lower=None,
        upper=None,
        sigma=None,
        guesses=None,
        full_ansatz=False
    ):
        r"""
        Fit the correlation function with Ni exponential terms
        for the imaginary part of the correlation function and Nr for the real.
        If no number of terms is provided, this function determines the number
        of exponents based on reducing the normalized root mean squared
        error below a certain threshold.

        Parameters
        ----------
        Nr : optional, int
            Number of exponents to use for the real part.
            If set to None it is determined automatically.
        Ni : optional, int
            Number of exponents terms to use for the imaginary part.
            If set to None it is found automatically.
        final_rmse : float
            Desired normalized root mean squared error. Only used if Ni or Nr
            are not specified.
        lower : list
            lower bounds on the parameters for the fit. A list of size 4 when
            full_ansatz is True and of size 3 when it is false,each value
            represents the lower bound for each parameter.

            The first and last terms describe the real and imaginary parts of
            the amplitude, the second the decay rate, and the third one the
            oscillation frequency. The lower bounds are considered to be
            the same for all Nr and Ni exponents. for example

            lower=[0,-1,1,1]

            would bound the real part of the amplitude to be bigger than 0,
            the decay rate to be higher than -1, and the oscillation frequency
            to be bigger than 1, and the imaginary part of the amplitude to
            be greater than 1
        upper : list
            upper bounds on the parameters for the fit, the structure is the
            same as the lower keyword.
        sigma : float
            uncertainty in the data considered for the fit, all data points are
            considered to have the same uncertainty.
        guesses : list
            Initial guesses for the parameters. Same structure as lower and
            upper.
        full_ansatz : bool
            Indicates whether to use the function

            .. math::
                C(t)= \sum_{k}a_{k}e^{-b_{k} t}e^{i c_{k} t}

            for the fitting of the correlation function (when False, the
            default value)  this function gives us
            faster fits,usually it is not needed to tweek
            guesses, sigma, upper and lower as defaults work for most
            situations.  When set to True one uses the function

            .. math::
                C(t)= \sum_{k}(a_{k}+i d_{k})e^{-b_{k} t}e^{i c_{k} t}

            Unfortunately this gives us significantly slower fits and some
            tunning of the guesses,sigma, upper and lower are usually needed.
            On the other hand, it can lead to better fits with lesser exponents
            specially for anomalous spectral densities such that
            $Im(C(0))\neq 0$. When using this with default values if the fit
            takes too long you should input guesses, lower and upper bounds,
            if you are not sure what to set them to it is useful to use the
            output of fitting with the other option as guesses for the fit.



        Note: If one of lower, upper, sigma, guesses is None, all are discarded

        Returns
        -------
        1. A Bosonic Bath created with the fit parameters from the original
          correlation function (that was provided or interpolated).
        2. A dictionary containing the following information about the fit:
            * Nr :
                The number of terms used to fit the real part of the
                correlation function.
            * Ni :
                The number of terms used to fit the imaginary part of the
                correlation function.
            * fit_time_real :
                The time the fit of the real part of the correlation function
                took in seconds.
            * fit_time_imag :
                The time the fit of the imaginary part of the correlation
                function took in seconds.
            * rsme_real :
                Normalized mean squared error obtained in the fit of the real
                part of the correlation function.
            * rsme_imag :
                Normalized mean squared error obtained in the fit of the
                imaginary part of the correlation function.
            * params_real :
                The fitted parameters (3N parameters) for the real part of the
                correlation function, it contains three lists one for each
                parameter, each list containing N terms.
            * params_imag :
                The fitted parameters (3N parameters) for the imaginary part
                of the correlation function, it contains three lists one for
                each parameter, each list containing N terms.
            * summary :
                A string that summarizes the information about the fit.
            """
        if full_ansatz:
            num_params = 4
        else:
            num_params = 3
        # Fit real part
        start_real = time()
        rmse_real, params_real = _run_fit(
            lambda *args: np.real(self._corr_approx(*args)),
            y=np.real(self._C_array), x=self._t, final_rmse=final_rmse,
            default_guess_scenario="correlation_real", N=Nr, sigma=sigma,
            guesses=guesses, lower=lower, upper=upper, n=num_params)
        end_real = time()

        # Fit imaginary part
        start_imag = time()
        rmse_imag, params_imag = _run_fit(
            lambda *args: np.imag(self._corr_approx(*args)),
            y=np.imag(self._C_array), x=self._t, final_rmse=final_rmse,
            default_guess_scenario="correlation_imag", N=Ni, sigma=sigma,
            guesses=guesses, lower=lower, upper=upper, n=num_params)
        end_imag = time()

        # Calculate Fit Times
        fit_time_real = end_real - start_real
        fit_time_imag = end_imag - start_imag

        # Generate summary
        Nr = len(params_real[0])
        Ni = len(params_imag[0])
        full_summary = _two_column_summary(
            params_real, params_imag, fit_time_real, fit_time_imag, Nr, Ni,
            rmse_imag, rmse_real, n=num_params)

        fitInfo = {"Nr": Nr, "Ni": Ni,
                   "fit_time_real": fit_time_real,
                   "fit_time_imag": fit_time_imag,
                   "rmse_real": rmse_real, "rmse_imag": rmse_imag,
                   "params_real": params_real,
                   "params_imag": params_imag, "summary": full_summary}
        self._generate_bath(params_real, params_imag, n=num_params)
        self.fitinfo = fitInfo

    def _generate_bath(self, params_real, params_imag, n=3):
        """
        Calculate the Matsubara coefficients and frequencies for the
        fitted underdamped oscillators and generate the corresponding bosonic
        bath.

        Parameters
        ----------
        params_real : :obj:`np.array.`
            array of shape (N,3) where N is the number of fitted terms
            for the real part.
        params_imag : np.imag
            array of shape (N,3) where N is the number of fitted terms
            for the imaginary part.

        Returns
        -------
        A bosonic Bath constructed from the fitted exponents.
        """
        if n == 4:
            a, b, c, d = params_real
            a2, b2, c2, d2 = params_imag
        else:
            a, b, c = params_real
            a2, b2, c2 = params_imag
            d = np.zeros(a.shape, dtype=int)
            d2 = np.zeros(a2.shape, dtype=int)

        # the 0.5 is from the cosine
        ckAR = [(x + 1j*y)*0.5 for x, y in zip(a, d)]
        # extend the list with the complex conjugates:
        ckAR.extend(np.conjugate(ckAR))
        vkAR = [-x - 1.0j * y for x, y in zip(b, c)]
        vkAR.extend([-x + 1.0j * y for x, y in zip(b, c)])

        # the 0.5 is from the sine
        ckAI = [-1j*(x + 1j*y)*0.5 for x, y in zip(a2, d2)]

        # extend the list with the complex conjugates:
        ckAI.extend(np.conjugate(ckAI))
        vkAI = [-x - 1.0j * y for x, y in zip(b2, c2)]
        vkAI.extend([-x + 1.0j * y for x, y in zip(b2, c2)])

        super().__init__(self.Q, ckAR, vkAR, ckAI, vkAI, T=self.T)
