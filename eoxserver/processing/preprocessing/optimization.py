#-------------------------------------------------------------------------------
# $Id$
#
# Project: EOxServer <http://eoxserver.org>
# Authors: Fabian Schindler <fabian.schindler@eox.at>
#          Stephan Meissl <stephan.meissl@eox.at>
#
#-------------------------------------------------------------------------------
# Copyright (C) 2012 EOX IT Services GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies of this Software or works derived from this Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#-------------------------------------------------------------------------------

import logging
import subprocess
import math
from itertools import product
import numpy

from eoxserver.contrib import gdal, gdal_array, osr, ogr
from eoxserver.processing.preprocessing.util import (
    get_limits, create_temp, copy_metadata, copy_projection, copy_nodatavalue
)
from eoxserver.resources.coverages.crss import (
    parseEPSGCode, fromShortCode, fromURL, fromURN, fromProj4Str
)


logger = logging.getLogger(__name__)

#===============================================================================
# Dataset Optimization steps
#===============================================================================


class DatasetOptimization(object):
    """ Abstract base class for dataset optimization steps. Each optimization
        step shall be callable and return the dataset or a copy thereof if
        necessary.
    """

    def __call__(self, ds):
        raise NotImplementedError


class ReprojectionOptimization(DatasetOptimization):
    """ Dataset optimization step to reproject the dataset into a predefined
        projection identified by an SRID.
    """

    def __init__(self, crs_or_srid):
        if isinstance(crs_or_srid, int):
            pass
        elif isinstance(crs_or_srid, basestring):
            crs_or_srid = parseEPSGCode(crs_or_srid, (fromShortCode, fromURL,
                                                      fromURN, fromProj4Str))
        else:
            raise ValueError("Unable to obtain CRS from '%s'." %
                             type(crs_or_srid).__name__)
        self.srid = crs_or_srid

    def __call__(self, src_ds):
        # setup
        src_sr = osr.SpatialReference()
        src_sr.ImportFromWkt(src_ds.GetProjection())

        dst_sr = osr.SpatialReference()
        dst_sr.ImportFromEPSG(self.srid)

        if src_sr.IsSame(dst_sr) and (src_ds.GetGeoTransform()[1] > 0) \
                and (src_ds.GetGeoTransform()[5] < 0):
            logger.info("Source and destination projection are equal and image "
                        "is not flipped. Thus, no reprojection is required.")
            return src_ds

        # create a temporary dataset to get information about the output size
        tmp_ds = gdal.AutoCreateWarpedVRT(src_ds, None, dst_sr.ExportToWkt(),
                                          gdal.GRA_Bilinear, 0.125)

        # create the output dataset
        dst_ds = create_temp(tmp_ds.RasterXSize, tmp_ds.RasterYSize,
                             src_ds.RasterCount,
                             src_ds.GetRasterBand(1).DataType)

        # initialize with no data
        for i in range(src_ds.RasterCount):
            src_band = src_ds.GetRasterBand(i+1)
            if src_band.GetNoDataValue() is not None:
                dst_band = dst_ds.GetRasterBand(i+1)
                dst_band.SetNoDataValue(src_band.GetNoDataValue())
                dst_band.Fill(src_band.GetNoDataValue())

        # reproject the image
        dst_ds.SetProjection(dst_sr.ExportToWkt())
        dst_ds.SetGeoTransform(tmp_ds.GetGeoTransform())

        gdal.ReprojectImage(src_ds, dst_ds,
                            src_sr.ExportToWkt(),
                            dst_sr.ExportToWkt(),
                            gdal.GRA_Bilinear)

        tmp_ds = None

        # copy the metadata
        copy_metadata(src_ds, dst_ds)

        return dst_ds


class BandSelectionOptimization(DatasetOptimization):
    """ Dataset optimization step which selects a number of bands and their
    respective scale and copies them to the result dataset.
    """

    def __init__(self, bands, datatype=gdal.GDT_Byte):
        # preprocess bands list
        # TODO: improve
        self.bands = map(lambda b: b if len(b) == 3 else (b[0], None, None),
                         bands)
        self.datatype = datatype

    def __call__(self, src_ds):
        dst_ds = create_temp(src_ds.RasterXSize, src_ds.RasterYSize,
                             len(self.bands), self.datatype)
        dst_range = get_limits(self.datatype)

        multiple, multiple_written = 0, False

        for dst_index, (src_index, dmin, dmax) in enumerate(self.bands, 1):
            # check if next band is equal
            if dst_index < len(self.bands) and \
                    (src_index, dmin, dmax) == self.bands[dst_index]:
                multiple += 1
                continue
            # check that src band is available
            if src_index > src_ds.RasterCount:
                continue

            # initialize with zeros if band is 0
            if src_index == 0:
                src_band = src_ds.GetRasterBand(1)
                data = numpy.zeros(
                    (src_band.YSize, src_band.XSize),
                    dtype=gdal_array.codes[self.datatype]
                )
                src_min, src_max = (0, 0)
            # use src_ds band otherwise
            else:
                src_band = src_ds.GetRasterBand(src_index)
                src_min, src_max = src_band.ComputeRasterMinMax()

            # get min/max values or calculate from band
            if dmin is None:
                dmin = get_limits(src_band.DataType)[0]
            elif dmin == "min":
                dmin = src_min
            if dmax is None:
                dmax = get_limits(src_band.DataType)[1]
            elif dmax == "max":
                dmax = src_max
            src_range = (float(dmin), float(dmax))

            block_x_size, block_y_size = 512, 512

            num_x = int(math.ceil(float(src_band.XSize) / block_x_size))
            num_y = int(math.ceil(float(src_band.YSize) / block_y_size))

            dst_band = dst_ds.GetRasterBand(dst_index)
            if src_band.GetNoDataValue() is not None:
                dst_band.SetNoDataValue(src_band.GetNoDataValue())

            for block_x, block_y in product(range(num_x), range(num_y)):
                offset_x = block_x * block_x_size
                offset_y = block_y * block_y_size
                size_x = min(src_band.XSize - offset_x, block_x_size)
                size_y = min(src_band.YSize - offset_y, block_y_size)
                data = src_band.ReadAsArray(
                    offset_x, offset_y, size_x, size_y
                )

                # perform clipping and scaling
                data = ((dst_range[1] - dst_range[0]) *
                        ((numpy.clip(data, dmin, dmax) - src_range[0]) /
                        (src_range[1] - src_range[0])))

                # set new datatype
                data = data.astype(gdal_array.codes[self.datatype])

                # write result
                dst_band.WriteArray(data, offset_x, offset_y)

                # write equal bands at once
                if multiple > 0:
                    for i in range(multiple):
                        dst_band_multiple = dst_ds.GetRasterBand(dst_index-1-i)
                        dst_band_multiple.WriteArray(data, offset_x, offset_y)
                    multiple_written = True

            if multiple_written:
                multiple = 0
                multiple_written = False

        copy_projection(src_ds, dst_ds)
        copy_metadata(src_ds, dst_ds)

        return dst_ds


class ColorIndexOptimization(DatasetOptimization):
    """ Dataset optimization step to replace the pixel color values with a color
        index. If no color palette is given (e.g: a VRT or any other dataset
        containing a color table), this step takes the first three bands and
        computes a median color table.
    """

    def __init__(self, palette_file=None):
        self.palette_file = palette_file

    def __call__(self, src_ds):
        dst_ds = create_temp(src_ds.RasterXSize, src_ds.RasterYSize,
                             1, gdal.GDT_Byte)

        if not self.palette_file:
            # create a color table as a median of the given dataset
            ct = gdal.ColorTable()
            gdal.ComputeMedianCutPCT(src_ds.GetRasterBand(1),
                                     src_ds.GetRasterBand(2),
                                     src_ds.GetRasterBand(3),
                                     256, ct)

        else:
            # copy the color table from the given palette file
            pct_ds = gdal.Open(self.palette_file)
            pct_ct = pct_ds.GetRasterBand(1).GetRasterColorTable()
            if not pct_ct:
                raise ValueError("The palette file '%s' does not have a Color "
                                 "Table." % self.palette_file)
            ct = pct_ct.Clone()
            pct_ds = None

        dst_ds.GetRasterBand(1).SetRasterColorTable(ct)
        gdal.DitherRGB2PCT(src_ds.GetRasterBand(1),
                           src_ds.GetRasterBand(2),
                           src_ds.GetRasterBand(3),
                           dst_ds.GetRasterBand(1), ct)

        copy_projection(src_ds, dst_ds)
        copy_metadata(src_ds, dst_ds)

        return dst_ds


class NoDataValueOptimization(DatasetOptimization):
    """ This optimization step assigns a no-data value to all raster bands in
        a dataset.
    """

    def __init__(self, nodata_values):
        self.nodata_values = nodata_values

    def __call__(self, ds):
        nodata_values = self.nodata_values
        if len(nodata_values) == 1:
            nodata_values = nodata_values * ds.RasterCount

        #TODO: bug, the same nodata value is set to all bands?

        for index, value in enumerate(nodata_values, start=1):
            try:
                ds.GetRasterBand(index).SetNoDataValue(value)
            except RuntimeError:
                pass  # TODO

        return ds


#===============================================================================
# Post-create optimization steps
#===============================================================================

class DatasetPostOptimization(object):
    """ Abstract base class for dataset post-creation optimization steps. These
        opotimizations are performed on the actually produced dataset. This is
        required by some optimization techiques.
    """

    def __call__(self, ds):
        raise NotImplementedError


class OverviewOptimization(DatasetPostOptimization):
    """ Dataset optimization step to add overviews to the dataset. This step may
        have to be applied after the dataset has been reprojected.
    """

    def __init__(self, resampling=None, levels=None, minsize=None):
        self.resampling = resampling
        self.levels = levels
        self.minsize = minsize

    def __call__(self, ds):
        levels = self.levels

        # calculate the overviews automatically.
        if not levels:
            desired_size = abs(self.minsize or 256)
            size = max(ds.RasterXSize, ds.RasterYSize)
            level = 1
            levels = []

            while size > desired_size:
                size /= 2
                level *= 2
                levels.append(level)

        logger.info("Building overview levels %s with resampling method '%s'."
                    % (", ".join(map(str, levels)), self.resampling))

        filename = ds.GetFileList()[0]
        process = subprocess.Popen(
            ["gdaladdo", "-q", "-clean", filename],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        out, err = process.communicate()
        for string in (out, err):
            for line in string.split("\n"):
                if line != '':
                    logger.info("gdaladdo output: %s" % line)

        if process.returncode != 0:
            logger.warning(
                "Deletion of overviews failed. (Returncode: %d)"
                % process.returncode
            )

        process = subprocess.Popen(
            ["gdaladdo", "-q", "-r", self.resampling or "nearest", filename]
            + [str(l) for l in levels],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        out, err = process.communicate()
        for string in (out, err):
            for line in string.split("\n"):
                if line != '':
                    logger.info("gdaladdo output: %s" % line)

        if process.returncode != 0:
            logger.warning(
                "Creation of overviews failed. (Returncode: %d)"
                % process.returncode
            )

        return ds


#===============================================================================
# AlphaBand Optimization
#===============================================================================

class AlphaBandOptimization(object):
    """ This optimization renders the footprint into the alpha channel of the
    image. """

    def __call__(self, src_ds, footprint_wkt):
        dt = src_ds.GetRasterBand(1).DataType
        if src_ds.RasterCount == 3:
            src_ds.AddBand(dt)
        elif src_ds.RasterCount == 4:
            pass  # okay
        else:
            raise Exception("Cannot add alpha band, as the current band number "
                            "'%d' does not match" % src_ds.RasterCount)

        # initialize the alpha band with zeroes (completely transparent)
        band = src_ds.GetRasterBand(4)
        band.Fill(0)

        # set up the layer with geometry
        ogr_ds = ogr.GetDriverByName('Memory').CreateDataSource('wkt')

        sr = osr.SpatialReference()
        sr.ImportFromEPSG(4326)
        layer = ogr_ds.CreateLayer('poly', srs=sr)

        feat = ogr.Feature(layer.GetLayerDefn())
        feat.SetGeometryDirectly(ogr.Geometry(wkt=footprint_wkt))
        layer.CreateFeature(feat)

        # rasterize the polygon, burning the opaque value into the alpha band
        gdal.RasterizeLayer(src_ds, [4], layer, burn_values=[get_limits(dt)[1]])
