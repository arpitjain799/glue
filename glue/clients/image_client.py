import logging

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize

from .modest_image import extract_matched_slices
from ..core.exceptions import IncompatibleAttribute
from ..core.data import Data
from ..core.subset import Subset, RoiSubsetState
from ..core.roi import PolygonalROI
from ..core.edit_subset_mode import EditSubsetMode
from ..core.util import color2rgb

from .viz_client import VizClient, init_mpl


class InvNormalize(Normalize):
    """ Simple wrapper to matplotlib Normalize object, that
    handles the case where vmax <= vmin """
    def __call__(self, value):
        if self.vmax <= self.vmin:
            self.vmax, self.vmin = self.vmin, self.vmax
            result = 1 - Normalize.__call__(self, value)
            self.vmax, self.vmin = self.vmin, self.vmax
        else:
            result = Normalize.__call__(self, value)
        return result


class LayerManager(object):
    def __init__(self, layer, axes):
        self.layer = layer
        self.artist = None
        self.component_id = None
        self._ax = axes

    def set_visible(self, state):
        raise NotImplementedError

    def delete_artist(self):
        raise NotImplementedError

    def update_artist(self, view):
        raise NotImplementedError

    def delete(self):
        self.delete_artist()


class DataLayerManager(LayerManager):
    def __init__(self, layer, axes):
        super(DataLayerManager, self).__init__(layer, axes)
        self.cmap = plt.cm.gray
        self.norm = InvNormalize()

    def update_artist(self, view):
        self.delete_artist()
        image = self.layer[view]
        self.artist = self._ax.imshow(image, cmap=self.cmap, norm=self.norm,
                                      interpolation='nearest', origin='lower',
                                      extent=_get_extent(view), zorder=0)

    def set_visible(self, state):
        if self.artist is None:
            return
        self.artist.set_visible(state)

    def set_norm(self, vmin, vmax):
        if vmin is not None:
            self.norm.vmin = vmin
        if vmax is not None:
            self.norm.vmax = vmax

    def delete_artist(self):
        if self.artist is None:
            return
        self.artist.remove()
        self.artist = None


class SubsetLayerManager(LayerManager):
    def __init__(self, layer, axes):
        super(SubsetLayerManager, self).__init__(layer, axes)

    def set_visible(self, state):
        if self.artist is None:
            return
        self.artist.set_visible(state)

    def is_visible(self):
        if self.artist is None:
            return False
        return self.artist.get_visible()

    def delete_artist(self):
        if self.artist is None:
            return

        self.artist.remove()
        self.artist = None

    def update_artist(self, view):
        subset = self.layer
        self.delete_artist()
        logging.debug("View into subset %s is %s", self.layer, view)

        try:
            mask = subset.to_mask(view[1:])
        except IncompatibleAttribute:
            return
        logging.debug("View mask has shape %s", mask.shape)

        #shortcut for empty subsets
        if not mask.any():
            return

        extent = _get_extent(view)
        r, g, b = color2rgb(self.layer.style.color)
        mask = np.dstack((r * mask, g * mask, b * mask, mask * .5))
        mask = (255 * mask).astype(np.uint8)
        self.artist = self._ax.imshow(mask, extent=extent,
                                      interpolation='nearest', origin='lower',
                                      zorder=5)


class ScatterLayerManager(LayerManager):
    def __init__(self, layer, axes):
        super(ScatterLayerManager, self).__init__(layer, axes)

    def set_visible(self, state):
        if self.artist is None:
            return
        self.artist.set_visible(state)

    def is_visible(self):
        return self.artist is not None and self.artist.get_visible()

    def delete_artist(self):
        if self.artist is None:
            return

        self.artist.remove()
        self.artist = None

    def update_artist(self, view, xatt, yatt):
        self.delete_artist()

        try:
            x = self.layer[xatt, view]
            y = self.layer[yatt, view]
        except IncompatibleAttribute:
            return

        self.artist, = self._ax.plot(x, y, 'o', c=self.layer.style.color,
                                     ms=self.layer.style.markersize * .5,
                                     mec='none',
                                     zorder=10)


class ImageClient(VizClient):

    def __init__(self, data, figure=None, axes=None):

        figure, axes = init_mpl(figure, axes)

        VizClient.__init__(self, data)

        self.layers = {}

        self.display_data = None
        self.display_attribute = None
        self._slice_ori = 2
        self._slice_ind = 0
        self._image = None
        self._view_window = None
        self._view = None

        self._ax = axes
        self._figure = figure

        self._cid = self._ax.figure.canvas.mpl_connect('button_release_event',
                                                       self._check_update)

        def log_view(event):
            self._view_window = _view_window(self._ax)

        self._cid2 = self._ax.figure.canvas.mpl_connect('button_press_event',
                                                        log_view)

    @property
    def is_3D(self):
        if not self.display_data:
            return False
        return len(self.display_data.shape) == 3

    @property
    def slice_ind(self):
        if self.is_3D:
            return self._slice_ind
        return None

    @property
    def image(self):
        return self._image

    @slice_ind.setter
    def slice_ind(self, value):
        if self.is_3D:
            self._slice_ind = value
            self._update_data_plot()
            self._update_subset_plots()
            self._redraw()
        else:
            raise IndexError("Cannot set slice for 2D image")

    def can_handle_data(self, data):
        return data.ndim in [2, 3]

    def _ensure_data_present(self, data):
        if data not in self.layers:
            self.add_layer(data)

    def _check_update(self, event):
        vw = _view_window(self._ax)
        if vw != self._view_window:
            self._update_data_plot()
            self._update_subset_plots()
            self._redraw()

    def set_data(self, data, attribute=None):
        self._ensure_data_present(data)

        if attribute:
            self.layers[data].component_id = attribute
        elif self.layers[data].component_id is None:
            self.layers[data].component_id = _default_component(data)
        attribute = self.layers[data].component_id

        self.display_data = data
        self.display_attribute = attribute
        self._update_data_plot(relim=True)
        self._update_visibilities()
        self._update_subset_plots()
        self._update_scatter_plots()
        self._redraw()

    def slice_bounds(self):
        if not self.is_3D:
            return (0, 0)
        if self._slice_ori == 2:
            return (0, self.display_data.shape[2] - 1)
        if self._slice_ori == 1:
            return (0, self.display_data.shape[1] - 1)
        if self._slice_ori == 0:
            return (0, self.display_data.shape[0] - 1)

    def set_slice_ori(self, ori):
        if not self.is_3D:
            raise IndexError("Cannot set orientation of 2D image")
        if ori not in [0, 1, 2]:
            raise TypeError("Orientation must be 0, 1, or 2")
        self._slice_ori = ori
        self.slice_ind = min(self.slice_ind, self.slice_bounds()[1])
        self.slice_ind = max(self.slice_ind, self.slice_bounds()[0])
        self._update_data_plot(relim=True)
        self._update_subset_plots()
        self._update_scatter_plots()

        self._redraw()

    def set_attribute(self, attribute):
        if not self.display_data or \
                attribute not in self.display_data.component_ids():
            raise IncompatibleAttribute(
                "Attribute not in data's attributes: %s" % attribute)
        self.display_attribute = attribute
        self.layers[self.display_data].component_id = attribute
        self._update_data_plot()
        self._redraw()

    def _redraw(self):
        """
        Re-render the screen
        """
        self._ax.figure.canvas.draw()

    def set_norm(self, vmin, vmax):
        if not self.display_data:
            return
        self.layers[self.display_data].set_norm(vmin, vmax)
        self._update_data_plot()
        self._redraw()

    def set_cmap(self, cmap):
        if not self.display_data:
            return
        self.layers[self.display_data].cmap = cmap
        self._update_data_plot()
        self._redraw()

    def _extract_slice_from_data(self, data=None):
        if data is None:
            result = self.display_data[self.display_attribute]
        else:
            result = data

        if not self.is_3D:
            return result
        if self._slice_ori == 2:
            result = result[:, :, self.slice_ind]
        elif self._slice_ori == 1:
            result = result[:, self.slice_ind, :]
        else:
            result = result[self.slice_ind, :, :]

        return result

    def _build_view(self, matched=False):
        att = self.display_attribute
        shp = self.display_data.shape
        shp_2d = _2d_shape(shp, self._slice_ori)
        x, y = np.s_[:], np.s_[:]
        if matched:
            v = extract_matched_slices(self._ax, shp_2d)
            x = slice(v[0], v[1], v[2])
            y = slice(v[3], v[4], v[5])

        if not self.is_3D:
            return (att, y, x)
        if self._slice_ori == 0:
            return (att, self.slice_ind, y, x)
        if self._slice_ori == 1:
            return (att, y, self.slice_ind, x)
        assert self._slice_ori == 2
        return (att, y, x, self.slice_ind)

    def _update_data_plot(self, relim=False):
        """
        Re-sync the main image and its subsets
        """

        if not self.display_data:
            return

        if relim:
            self.relim()

        view = self._build_view(matched=True)
        self._view = view
        self.layers[self.display_data].update_artist(view)
        self._image = self.display_data[view]

    def _update_visibilities(self):
        for layer in self.layers:
            self.layers[layer].set_visible(layer.data is self.display_data)

    def relim(self):
        shp = _2d_shape(self.display_data.shape, self._slice_ori)
        self._ax.set_xlim(0, shp[1])
        self._ax.set_ylim(0, shp[0])

    def _update_axis_labels(self):
        self._ax.set_xlabel('X')
        self._ax.set_ylabel('Y')

    def _update_subset_single(self, s, redraw=False):
        """
        Update the location and visual properties
        of each point in a single subset

        Parameters:
        ----------
        s: A subset instance
        The subset to refresh.

        """
        logging.debug("update subset single: %s", s)

        if s not in self.layers:
            return
        if isinstance(self.layers[s], ScatterLayerManager):
            self._update_scatter_layer(s, redraw)
            return

        if s.data is not self.display_data:
            return

        view = self._build_view(matched=True)
        self.layers[s].update_artist(view)
        if redraw:
            self._redraw()

    def _apply_roi(self, roi):
        data = self.display_data
        if data is None:
            return

        subset_state = RoiSubsetState()
        xroi, yroi = roi.to_polygon()
        x, y = self._get_axis_components()
        subset_state.xatt = x
        subset_state.yatt = y
        subset_state.roi = PolygonalROI(xroi, yroi)
        mode = EditSubsetMode()
        mode.combine(data.edit_subset, subset_state)

    def _horizontal_axis_index(self):
        """Which index (in numpy convention - zyx) does the horizontal
        axis coorespond to?"""
        if not self.is_3D or self._slice_ori == 2:
            return 1
        return 2

    def _vertical_axis_index(self):
        """Which index (in numpy convention - zyx) does the vertical
        axis coorespond to?"""
        if self.is_3D and self._slice_ori == 0:
            return 1
        return 0

    def _get_axis_components(self):
        data = self.display_data
        ids = [self._horizontal_axis_index(), self._vertical_axis_index()]
        return map(data.get_pixel_component_id, ids)

    def _remove_subset(self, message):
        self.delete_layer(message.sender)

    def delete_layer(self, layer):
        if layer not in self.layers:
            return
        manager = self.layers.pop(layer)
        manager.delete()

        if layer is self.display_data:
            self.display_data = None

        if isinstance(layer, Data):
            for subset in layer.subsets:
                self.delete_layer(subset)

        self._redraw()

    def _remove_data(self, message):
        self.delete_layer(message.data)
        for s in message.data.subsets:
            self.delete_layer(s)

    def init_layer(self, layer):
        self.add_layer(layer)

    def add_layer(self, layer):
        if layer in self.layers:
            return

        if layer.data not in self.data:
            raise TypeError("Data not managed by client's data collection")

        if not self.can_handle_data(layer.data):
            logging.warning("Cannot visulize data: %s. Aborting", layer.data)
            return

        if isinstance(layer, Data):
            self.layers[layer] = DataLayerManager(layer, self._ax)
            for s in layer.subsets:
                self.add_layer(s)
        elif isinstance(layer, Subset):
            self.layers[layer] = SubsetLayerManager(layer, self._ax)
            self._update_subset_single(layer)
        else:
            raise TypeError("Unrecognized layer type: %s" % type(layer))

    def add_scatter_layer(self, layer):
        if layer in self.layers:
            return

        self.layers[layer] = ScatterLayerManager(layer, self._ax)
        self._update_scatter_layer(layer, redraw=True)

    def _update_scatter_layer(self, layer, redraw=False):
        if not self._is_scatter_layer(layer):
            return

        xatt, yatt = self._get_plot_attributes()
        self.layers[layer].update_artist(None, xatt, yatt)
        if redraw:
            self._redraw()

    def _is_scatter_layer(self, layer):
        return layer in self.layers and isinstance(self.layers[layer],
                                                   ScatterLayerManager)

    def _get_plot_attributes(self):
        if self.display_data is None:
            return
        y, x = _slice_axis(self.display_data.shape, self._slice_ori)
        ids = self.display_data.pixel_component_ids
        return ids[x], ids[y]

    def _update_scatter_plots(self):
        for layer in self.layers:
            if self._is_scatter_layer(layer):
                self._update_scatter_layer(layer)


def _get_extent(view):
    sy, sx = [s for s in view if isinstance(s, slice)]
    return (sx.start, sx.stop, sy.start, sy.stop)


def _2d_shape(shape, slice_ori):
    """Return the shape of the 2D slice through a 2 or 3D image"""
    if len(shape) == 2:
        return shape
    if slice_ori == 0:
        return shape[1:]
    if slice_ori == 1:
        return shape[0], shape[2]
    assert slice_ori == 2
    return shape[0:2]


def _slice_axis(shape, slice_ori):
    if len(shape) == 2:
        return 0, 1
    if slice_ori == 0:
        return 1, 2
    if slice_ori == 1:
        return 0, 2
    assert slice_ori == 2
    return 0, 1


def _view_window(ax):
    """ Return a tuple describing the view window of an axes object.

    The contents should not be used directly, Rather, several
    return values should be compared with == to determine if the
    window has been panned/zoomed
    """
    ext = ax.transAxes.transform([1, 1]) - ax.transAxes.transform([0, 0])
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    result = xlim[0], ylim[0], xlim[1], ylim[1], ext[0], ext[1]
    logging.debug("view window: %s", result)
    return result


def _default_component(data):
    """Choose a default ComponentID to display for data

    Returns PRIMARY if present
    """
    cid = data.find_component_id('PRIMARY')
    if cid is not None:
        return cid
    return data.component_ids()[0]
