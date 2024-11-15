import numpy as np
import math
from utils.math_libs import *
from vtkmodules.all import vtkImageData, vtkStructuredGrid, vtkUnstructuredGrid, vtkPolyData, vtkTransform, \
    vtkTransformFilter, vtkBoundingBox, vtkDataSet, VTK_DOUBLE, VTK_INT, vtkLookupTable, vtkColorTransferFunction, \
    vtkImagePermute, vtkProbeFilter, vtkImageMapToColors, vtkPNGWriter, vtkCellArray, vtkPoints, vtkRectilinearGrid, \
    vtkImageDataGeometryFilter, vtkImageToPolyDataFilter, vtkPolyDataMapper, vtkImageStencil, vtkIdList, vtkPointData \
    , vtkDataSetSurfaceFilter, vtkClipDataSet, vtkPlane, vtkTriangleFilter, vtkPolygon, vtkDataSetReader, vtkIntArray \
    , vtkCleanPolyData, vtkOBBTree

from vtkmodules.util import numpy_support
import os
import vtkmodules.all as vtk
from vtkmodules.all import vtkSurfaceReconstructionFilter, vtkAppendPolyData
import pyvista as pv
import copy
import scipy.spatial as spt
import collections.abc
import geopandas as gpd
import shapely as sy
from utils.math_libs import remove_duplicate_points, add_point_to_point_set_if_no_duplicate, check_triangle_box_overlap
from tqdm import tqdm
import concurrent.futures


def create_box_poly_data_from_bounds(bounds):
    min_x, max_x, min_y, max_y, min_z, max_z = bounds[0], bounds[1], bounds[2], bounds[3], bounds[4], bounds[5]
    point_array = [
        [min_x, min_y, min_z], [max_x, min_y, min_z], [max_x, max_y, min_z], [min_x, max_y, min_z],
        [min_x, min_y, max_z], [max_x, min_y, max_z], [max_x, max_y, max_z], [min_x, max_y, max_z]
    ]
    faces = [
        [4, 0, 1, 2, 3], [4, 4, 5, 6, 7], [4, 0, 1, 5, 4], [4, 3, 2, 6, 7], [4, 0, 3, 7, 4], [4, 1, 2, 6, 5]
    ]
    faces = np.hstack(faces)
    poly_box = pv.PolyData(point_array, faces)
    return poly_box


def fill_1(grid):
    out_grid = copy.deepcopy(grid)
    left_slice = out_grid.vtk_data.slice(normal='y')
    left_points = left_slice.cell_centers().points.tolist()
    cells_series = np.full((len(out_grid.grid_points),), fill_value=-1)
    grid_bounds = out_grid.bounds
    y_max = grid_bounds[3]
    y_min = grid_bounds[2]
    pbr = tqdm(enumerate(left_points), total=len(left_points))
    for it, hp in pbr:
        pos_a = copy.deepcopy(left_points[it])
        pos_b = copy.deepcopy(left_points[it])
        pos_a[1] = y_max
        pos_b[1] = y_min
        pid = out_grid.vtk_data.find_cells_along_line(pointa=pos_a, pointb=pos_b)
        pid = np.array(pid, dtype=int)
        line_points = out_grid.grid_points[pid]
        line_series = out_grid.vtk_data.cell_data['Scalar Field'][pid]
        line_points_sort_ind = np.argsort(line_points[:, 1])
        line_points = line_points[line_points_sort_ind[::-1]]
        line_series = line_series[line_points_sort_ind[::-1]]
        pid = pid[line_points_sort_ind[::-1]]
        new_line_series = copy.deepcopy(line_series)
        if -3 in line_series and len(np.unique(line_series)) > 1:
            series_list = []
            for label_id, label in enumerate(line_series):
                if label != -3:
                    series_list.append((label_id, label))
            for line_id, label in enumerate(line_series):
                if label == -3:
                    for li, label_record in enumerate(series_list):
                        if line_id < label_record[0] and li > 0:
                            break
                        # if li < len(series_list) - 1 and label_record[0] < line_id < series_list[li + 1][0] and \
                        #         series_list[li + 1][1] == -2 and label_record[1] != -2:
                        #     new_line_series[line_id] = label_record[1]
                        #     break
                        # if li < len(series_list) - 1 and label_record[0] < line_id < series_list[li + 1][0] and \
                        #         label_record[1] == -2 and series_list[li + 1][1] != -2:
                        #     new_line_series[line_id] = series_list[li + 1][1]
                        #     break
                        if li < len(series_list) - 1 and label_record[0] < line_id < series_list[li + 1][0] and \
                                label_record[1] == series_list[li + 1][1] and label_record[1] != -2:
                            new_line_series[line_id] = series_list[li + 1][1]
                            break
                        if li == 0 and label_record[0] > line_id and label_record[1] != -2:
                            new_line_series[line_id] = label_record[1]
                            break
                        if li == len(series_list) - 1 and label_record[0] < line_id and label_record[1] != -2:
                            new_line_series[line_id] = label_record[1]
                            break
                        if li < len(series_list) - 1 and label_record[0] < line_id < series_list[li + 1][0]:
                            if label_record[1] != -2:
                                new_line_series[line_id] = label_record[1]
                                break
                            if series_list[li + 1][1] != -2:
                                new_line_series[line_id] = series_list[li + 1][1]
                                break
        cells_series[pid] = new_line_series
    out_grid.vtk_data.cell_data['Scalar Field'] = cells_series
    return out_grid


# 从上往下遍历
def fill_cell_values_with_surface_grid(grid):
    # 获取顶部剖面

    out_grid = copy.deepcopy(grid)
    horizon_slice = out_grid.vtk_data.slice(normal='z')
    horizon_points = horizon_slice.cell_centers().points.tolist()
    cells_series = np.full((len(out_grid.grid_points),), fill_value=-1)
    grid_bounds = out_grid.bounds
    z_max = grid_bounds[5]
    z_min = grid_bounds[4]
    pbr = tqdm(enumerate(horizon_points), total=len(horizon_points))
    unfill_points = []
    # out_grid.vtk_data.plot()
    for it, hp in pbr:
        pos_a = copy.deepcopy(horizon_points[it])
        pos_b = copy.deepcopy(horizon_points[it])
        pos_a[2] = z_max
        pos_b[2] = z_min
        pid = out_grid.vtk_data.find_cells_along_line(pointa=pos_a, pointb=pos_b)
        pid = np.array(pid, dtype=int)
        line_points = out_grid.grid_points[pid]
        line_series = out_grid.vtk_data.cell_data['Scalar Field'][pid]
        line_points_sort_ind = np.argsort(line_points[:, 2])
        line_points = line_points[line_points_sort_ind[::-1]]
        line_series = line_series[line_points_sort_ind[::-1]]
        pid = pid[line_points_sort_ind[::-1]]
        new_line_series = copy.deepcopy(line_series)

        if -1 in line_series and len(np.unique(line_series)) > 1:
            series_list = []
            for label_id, label in enumerate(line_series):
                if label != -1:
                    series_list.append((label_id, label))
            for line_id, label in enumerate(line_series):
                if label == -1:
                    check_flag = False
                    for label_record in series_list:
                        if line_id < label_record[0]:
                            if label_record[1] == -2:
                                new_line_series[line_id] = -3
                            else:
                                new_line_series[line_id] = label_record[1]
                            check_flag = True
                            break
        else:
            unfill_points.append(it)
        cells_series[pid] = new_line_series
    unfill_cells_ids = np.argwhere(cells_series == -1).flatten()
    # if len(unfill_cells_ids) > 0:
    #     filled_cells_ids = np.array(list(set(np.arange(len(cells_series))) - set(unfill_cells_ids)))
    #     ckt = spt.cKDTree(grid.grid_points[filled_cells_ids])
    #     d, pid = ckt.query(grid.grid_points[unfill_cells_ids])
    #     check_series = cells_series[filled_cells_ids]
    #     unfill_series = check_series[pid]
    #     cells_series[unfill_cells_ids] = unfill_series
    out_grid.vtk_data.cell_data['Scalar Field'] = cells_series
    return out_grid


# 面数据与网格数据求交
# poly_surf: PolyData面数据
# check_level: 构建obbtree的级别，默认为0，构建最初级
# grid: vtk网格
def poly_surf_intersect_with_grid(poly_surf: pv.PolyData, grid, check_level=0):
    vtk_obbtree_0 = vtkOBBTree()
    vtk_obbtree_0.SetDataSet(poly_surf)
    vtk_obbtree_0.BuildLocator()
    obb_poly = vtkPolyData()
    max_level = vtk_obbtree_0.GetLevel()
    if check_level is None:
        check_level = max_level
    else:
        if check_level > max_level:
            check_level = 0
    vtk_obbtree_0.GenerateRepresentation(check_level, obb_poly)
    obb_poly = pv.wrap(obb_poly)
    select_cell_ids = []
    if isinstance(grid, (pv.RectilinearGrid, pv.UnstructuredGrid, pv.StructuredGrid)):
        gird_points_poly = pv.PolyData(grid.points)
        select_cells = gird_points_poly.select_enclosed_points(surface=obb_poly, check_surface=True,
                                                               tolerance=0.000000001)
        rect_point_ids = select_cells.point_data['SelectedPoints']
        rect_point_ids = np.argwhere(rect_point_ids > 0).flatten()
        select_points = grid.extract_points(ind=rect_point_ids)
        rect_point_ids = grid.find_containing_cell(select_points.cell_centers().points)
        print('Computing...')
        face_points = np.array([poly_surf.get_cell(index=face_id).points for face_id in np.arange(poly_surf.n_cells)])
        # face_points = np.array(face_points, dtype=np.float32)
        pbr = tqdm(enumerate(rect_point_ids), total=len(rect_point_ids), position=0, leave=True)
        for it, cell_id in pbr:
            cell = grid.extract_cells([cell_id])
            check_intersect = check_triangle_box_overlap(tri_points=face_points, voxel_points=cell.points)
            if check_intersect:
                select_cell_ids.append(cell_id)
        return select_cell_ids


def voxelize(mesh, density=None, check_surface=True, tolerance=0.000000001):
    if not pv.is_pyvista_dataset(mesh):
        mesh = pv.wrap(mesh)
    # mesh.plot()
    if density is None:
        density = mesh.length / 100
    if isinstance(density, (int, float, np.number)):
        density_x, density_y, density_z = [density] * 3
    elif isinstance(density, (collections.abc.Sequence, np.ndarray)):
        density_x, density_y, density_z = density
    else:
        raise TypeError(f'Invalid density {density!r}, expected number or array-like.')

    # check and pre-process input mesh
    surface = mesh.extract_geometry()  # filter preserves topology

    # surface.plot()

    if not surface.faces.size:
        # we have a point cloud or an empty mesh
        raise ValueError('Input mesh must have faces for voxelization.')
    if not surface.is_all_triangles:
        # reduce chance for artifacts, see gh-1743
        surface.triangulate(inplace=True)
        surface.clean()

    x_min, x_max, y_min, y_max, z_min, z_max = mesh.bounds
    x = np.arange(x_min, x_max, density_x)
    y = np.arange(y_min, y_max, density_y)
    z = np.arange(z_min, z_max, density_z)
    x, y, z = np.meshgrid(x, y, z, indexing='ij')
    # indexing='ij' is used here in order to make grid and ugrid with x-y-z ordering, not y-x-z ordering
    # see https://github.com/pyvista/pyvista/pull/4365

    # Create unstructured grid from the structured grid
    grid = pv.StructuredGrid(x, y, z)
    ugrid = pv.UnstructuredGrid(grid)
    # pl = pv.Plotter()
    # pl.add_mesh(ugrid)
    # pl.add_mesh(surface)
    # pl.show()
    # get part of the mesh within the mesh's bounding surface.
    # ugrid.plot()
    selection = ugrid.select_enclosed_points(surface, tolerance=tolerance, check_surface=check_surface)
    mask = selection.point_data['SelectedPoints'].view(np.bool_)

    # extract cells from point indices
    vox = ugrid.extract_points(mask)
    return vox


def read_dxf_surface(geom_file_path: str):
    gdf = gpd.read_file(geom_file_path)
    geoms = gdf['geometry']
    layer = gdf['Layer']
    points_array2 = []
    for geom in geoms:
        if isinstance(geom, sy.Polygon):
            coords = [list(coord) for coord in geom.exterior.coords]
            points_3d, _ = remove_duplicate_points(points_3d=coords, is_remove=True)
            points_3d = [list(point) for point in points_3d]
            points_array2.append(points_3d)
        else:
            raise ValueError('Geom Type not support.')
    points_array2, points_ids_list = get_poly_points_topo(points_array2)
    faces = []
    for points_ids in points_ids_list:
        face = [len(points_ids)]
        face.extend(points_ids)
        faces.append(face)
    faces = np.hstack(faces)
    surface_polydata = pv.PolyData(np.array(points_array2), faces)
    # surface_polydata.cell_data['layer'] = layer  # 与更新后的cell数量不匹配
    return surface_polydata


# points_array 二维点集数组，每一组点构成一个多边形
def get_poly_points_topo(points_array2, check=3):
    from data_structure.points import PointSet
    points_set = PointSet()
    points_ids_list = []
    for i, points_list in enumerate(points_array2):
        points_ids = []
        for j, point in enumerate(points_list):
            _, p_id = points_set.append_search_point_without_labels(insert_point=point)
            points_ids.append(p_id)
        if i == 0:
            check = len(points_ids)
        else:
            if check != len(points_ids):
                continue
        points_ids_list.append(points_ids)
    return points_set.points, points_ids_list


# 通过传入三维点，创建多边形面，传入的点是排序好的环
def create_polygon_with_sorted_points_3d(points_3d):
    tri_filter = vtkTriangleFilter()
    pts = vtkPoints()
    polygon = vtkPolygon()
    polygon.GetPointIds().SetNumberOfIds(len(points_3d))
    for k, l in enumerate(points_3d):
        pts.InsertNextPoint(l)
        polygon.GetPointIds().SetId(k, k)
    polygons = vtkCellArray()
    polygons.InsertNextCell(polygon)
    polygon_poly = vtkPolyData()
    polygon_poly.SetPoints(pts)
    polygon_poly.SetPolys(polygons)
    tri_filter.SetInputData(polygon_poly)
    tri_filter.Update()
    polygon_poly_filtered = tri_filter.GetOutput()
    pp = pv.wrap(polygon_poly_filtered)
    return pp


# 隐式表面重建，根据一堆网格点来隐式地构建表面， 待修改
def create_implict_surface_reconstruct(points, sample_spacing,
                                       neighbour_size=20) -> pv.PolyData:
    surface = vtkSurfaceReconstructionFilter()
    poly_data = vtkPolyData()
    v_points = vtkPoints()
    v_points.SetData(numpy_support.numpy_to_vtk(points))
    poly_data.SetPoints(v_points)
    surface.SetInputData(poly_data)
    surface.SetNeighborhoodSize(neighbour_size)
    surface.SetSampleSpacing(sample_spacing)
    surface.Update()
    surface = pv.wrap(surface)
    return surface


# 根据凸包创建封闭面
def create_closed_surface_by_convexhull_2d(bounds: np.ndarray, convexhull_2d: np.ndarray):
    top_surface_points = copy.deepcopy(convexhull_2d)
    top_surface_points[:, 2] = bounds[5]  # z_max
    bottom_surface_points = copy.deepcopy(convexhull_2d)
    bottom_surface_points[:, 2] = bounds[4]  # z_min
    # 面三角化
    surface_points = np.concatenate((top_surface_points, bottom_surface_points), axis=0)
    # 顶面
    pro_point_2d = top_surface_points[:, 0:2]
    points_num = len(top_surface_points)
    tri = spt.Delaunay(pro_point_2d)
    tet_list = tri.simplices
    faces_top = []
    for it, tet in enumerate(tet_list):
        face = np.int64([3, tet[0], tet[1], tet[2]])
        faces_top.append(face)
    faces_top = np.int64(faces_top)
    # 底面的组织与顶面相同，face中的点号加一个points_num
    faces_bottom = []
    for it, face in enumerate(faces_top):
        face_new = copy.deepcopy(face)
        face_new[1:4] = np.add(face[1:4], points_num)
        faces_bottom.append(face_new)
    faces_bottom = np.int64(faces_bottom)
    faces_total = np.concatenate((faces_top, faces_bottom), axis=0)
    # 侧面
    # 需要先将三维度点投影到二维，上下面构成一个矩形，三角化
    surf_line_pnt_id = list(np.arange(points_num))
    surf_line_pnt_id.append(0)  # 环状线，首尾相连
    surf_line_pnt_id_0 = copy.deepcopy(surf_line_pnt_id)  #
    surf_line_pnt_id_0 = np.add(surf_line_pnt_id_0, points_num)
    surf_line_pnt_id_total = np.concatenate((surf_line_pnt_id, surf_line_pnt_id_0), axis=0)
    top_line = []
    bottom_line = []
    for lit in np.arange(points_num + 1):
        xy_top = np.array([lit, bounds[5]])
        xy_bottom = np.array([lit, bounds[4]])
        top_line.append(xy_top)
        bottom_line.append(xy_bottom)
    top_line = np.array(top_line)
    bottom_line = np.array(bottom_line)
    line_pnt_total = np.concatenate((top_line, bottom_line), axis=0)
    # 矩形三角化
    tri = spt.Delaunay(line_pnt_total)
    tet_list = tri.simplices
    faces_side = []
    for it, tet in enumerate(tet_list):
        item_0 = tet[0]
        item_1 = tet[1]
        item_2 = tet[2]
        face = np.int64(
            [3, surf_line_pnt_id_total[item_0], surf_line_pnt_id_total[item_1], surf_line_pnt_id_total[item_2]])
        faces_side.append(face)
    faces_side = np.int64(faces_side)
    faces_total = np.concatenate((faces_total, faces_side), axis=0)
    convex_surface = pv.PolyData(surface_points, faces=faces_total)
    line_boundary = []
    line_top = [len(surf_line_pnt_id)]
    line_bottom = [len(surf_line_pnt_id)]
    for lid in np.arange(len(surf_line_pnt_id)):
        line_top.append(surf_line_pnt_id[lid])
        line_bottom.append(surf_line_pnt_id_0[lid])
        line_of_side = [2, surf_line_pnt_id[lid], surf_line_pnt_id_0[lid]]
        line_boundary.append(np.int64(line_of_side))
    line_top = np.int64(line_top)
    line_bottom = np.int64(line_bottom)
    line_boundary.append(line_top)
    line_boundary.append(line_bottom)
    line_boundary = np.concatenate(line_boundary, axis=0)
    grid_outline = pv.PolyData(surface_points, lines=line_boundary)
    return convex_surface, grid_outline


# 创建一个封闭的圆柱面
def create_closed_cylinder_surface(top_point: np.ndarray, bottom_point: np.ndarray, radius: float, segment_num=10):
    # line_direction = np.subtract(top_point, bottom_point)
    # line_direction_norm = line_direction / np.linalg.norm(line_direction)
    height = np.linalg.norm(top_point - bottom_point)
    t = np.linspace(0, 2 * np.pi, segment_num)
    top_line_points_x = top_point[0] + radius * np.cos(t)
    top_line_points_y = top_point[1] + radius * np.sin(t)
    top_line_points_z = np.full_like(top_line_points_x, fill_value=top_point[2])
    top_line_points = np.array(list(zip(top_line_points_x, top_line_points_y, top_line_points_z)))
    circle_polygon = create_polygon_with_sorted_points_3d(points_3d=top_line_points)
    circle_polygon = circle_polygon.triangulate()
    cylinder_surface = circle_polygon.extrude((0, 0, -height), capping=True)
    return cylinder_surface


# 从 pv.PolyData类型转为 pv.UnstructredGrid类型
def vtk_polydata_to_vtk_unstructured_grid(poly_data: pv.PolyData):
    points = poly_data.GetPoints()
    points_num = poly_data.GetNumberOfPoints()
    vertexes = poly_data.GetVerts()
    lines = poly_data.GetLines()
    polys = poly_data.GetPolys()
    strips = poly_data.GetStrips()
    vertexs_num = poly_data.GetNumberOfCells()
    lines_num = poly_data.GetNumberOfLines()
    polys_num = poly_data.GetNumberOfPolys()
    strips_num = poly_data.GetNumberOfStrips()
    cells_num = poly_data.GetNumberOfCells()
    if points_num < 1 or (vertexs_num + lines_num + polys_num + strips_num) < 1:
        raise ValueError('The input vtk_poly_data is empty.')
    ugrid = vtkUnstructuredGrid()
    ugrid.SetPoints(points)
    for id in range(cells_num):
        pt_ids = vtkIdList()
        poly_data.GetCellPoints(id, pt_ids)
        cell_type = poly_data.GetCellType(id)
        ugrid.InsertNextCell(cell_type, pt_ids)
    return pv.wrap(ugrid)


# u_grid : pv.UnstructuredGrid or pv.RectilinearGrid
def vtk_unstructured_grid_to_vtk_polydata(u_grid):
    surface_filter = vtkDataSetSurfaceFilter()
    surface_filter.SetInputData(u_grid)
    surface_filter.Update()
    poly_data = surface_filter.GetOutput()
    return pv.wrap(poly_data)


def vtk_polydata_to_vtk_imagedata(poly_data: pv.PolyData):
    pass


# 可以自由创建 1维、2维、3维网格，如果是规则沿轴向，则只需要dim和bounds参数，也可以通过分割间断点序列xx,yy,zz来自定义网格
def create_vtk_grid_by_rect_bounds(dim: np.ndarray = None, bounds: np.ndarray = None,
                                   grid_buffer_xy=0) -> pv.RectilinearGrid:
    if dim is None or bounds is None:
        raise ValueError('Bounds array can not be None')
    else:
        nx = dim[0]
        ny = dim[1]
        nz = dim[2]
        min_x = bounds[0] - grid_buffer_xy
        max_x = bounds[1] + grid_buffer_xy
        min_y = bounds[2] - grid_buffer_xy
        max_y = bounds[3] + grid_buffer_xy
        min_z = bounds[4]
        max_z = bounds[5]
        xrng = np.linspace(start=min_x, stop=max_x, num=nx)
        yrng = np.linspace(start=min_y, stop=max_y, num=ny)
        zrng = np.linspace(start=min_z, stop=max_z, num=nz)
        vtk_grid = pv.RectilinearGrid(xrng, yrng, zrng)
        return vtk_grid


# 在规则格网的基础上，通过一个2d凸包范围切割格网
def create_vtk_grid_by_boundary(dims: np.ndarray, bounds: np.ndarray
                                , convexhull_2d: np.ndarray, cell_density: np.ndarray = None):
    convex_surface, grid_outline = create_closed_surface_by_convexhull_2d(bounds=bounds
                                                                          , convexhull_2d=convexhull_2d)
    if cell_density is None:
        if dims is None:
            raise ValueError('Need to input dims parameters.')
        x_r = (bounds[1] - bounds[0]) / dims[0]
        y_r = (bounds[3] - bounds[2]) / dims[1]
        z_r = (bounds[5] - bounds[4]) / dims[2]
        cell_density = np.array([x_r, y_r, z_r])
    sample_grid = pv.voxelize(convex_surface, density=cell_density)
    return sample_grid, grid_outline


def create_continuous_property_vtk_array(name: str, arr: np.ndarray):
    vtk_arr = numpy_support.numpy_to_vtk(arr, deep=True, array_type=VTK_DOUBLE)
    vtk_arr.SetName(name)
    if arr.ndim == 2 and arr.shape[1] != 1:
        vtk_arr.SetNumberOfComponents(arr.shape[1])
    return vtk_arr


def create_discrete_property_vtk_array(name: str, arr: np.ndarray):
    vtk_arr = numpy_support.numpy_to_vtk(arr, deep=True, array_type=VTK_INT)
    vtk_arr.SetName(name)
    if arr.ndim == 2 and arr.shape[1] != 1:
        vtk_arr.SetNumberOfComponents(arr.shape[1])
    return vtk_arr


def convert_continuous_probabilities_to_class_integer(prop_arr):
    assert prop_arr.ndim == 2, "property array is not a 2D array. Each row a vector of continuous prob"
    return np.argmax(prop_arr, axis=1)


def add_vtk_data_array_to_vtk_object(vtk_object, vtk_array):
    if type(vtk_object) == vtkStructuredGrid or \
            type(vtk_object) == vtkUnstructuredGrid or \
            type(vtk_object) == vtkPolyData or \
            type(vtk_object) == pv.PolyData:
        assert vtk_object.GetNumberOfPoints() == vtk_array.GetNumberOfTuples(), \
            "Num of Tuples is different than number of points on vtk object"
        vtk_object.GetPointData().AddArray(vtk_array)
    elif type(vtk_object) == vtkImageData:
        assert vtk_object.GetNumberOfCells() == vtk_array.GetNumberOfTuples(), \
            "Num of Tuples is different than number of cells on vtk object"
        vtk_object.GetCellData().AddArray(vtk_array)
    elif type(vtk_object) == pv.RectilinearGrid or \
            type(vtk_object) == pv.UnstructuredGrid:
        scalar_name = vtk_array.GetName()
        scalars = numpy_support.vtk_to_numpy(vtk_array)
        vtk_object[scalar_name] = scalars
    else:
        raise ValueError("vtk_object is not a vtkStructuredGrid | vtkImageData | vtkUnstructuredGrid")
    return vtk_object


def add_np_property_to_vtk_object(vtk_object, prop_name, prop_arr, continuous=True):
    if continuous:
        add_vtk_data_array_to_vtk_object(vtk_object, create_continuous_property_vtk_array(prop_name, prop_arr))
    else:
        add_vtk_data_array_to_vtk_object(vtk_object, create_discrete_property_vtk_array(prop_name, prop_arr))


# 水平和垂直方向拉伸  # vertically_ # horizontal_ 比例变换
def exaggerate_vtk_object(vtk_object, horizontal_x_exaggeration=1, horizontal_y_exaggeration=1
                          , vertical_exaggeration=1):
    transform = vtkTransform()
    transform.Scale(horizontal_x_exaggeration, horizontal_y_exaggeration, vertical_exaggeration)
    transform.Update()
    transformFilter = vtkTransformFilter()
    transformFilter.SetTransform(transform)
    transformFilter.SetInputData(vtk_object)
    transformFilter.Update()
    return pv.wrap(transformFilter.GetOutput())


def get_resultant_bounds_from_vtk_objects(*vtk_objects, xy_buffer=0, z_buffer=0):
    """
    Given multiple vtk based datasets find the resulting bounding box for all the data. The computed bounds:
    [xmin, xmax, ymin, ymax, zmin, zmax] will be the resulting bounding box bounds plus a buffer.
    """

    assert vtk_objects, "There are no objects supplied to function get_resultant_bounds_from_vtk_objects"

    bounding_box = vtkBoundingBox()

    for vtk_object in vtk_objects:
        assert isinstance(vtk_object, vtkDataSet), 'Inputted object is supposed to be a subclass of vtkDataset,' \
                                                   ' however got a {0} instead'.format(type(vtk_object))
        bounding_box.AddBounds(vtk_object.GetBounds())

    bounds_min = bounding_box.GetMinPoint()
    bounds_max = bounding_box.GetMaxPoint()
    data_bounds = [bounds_min[0],
                   bounds_max[0],
                   bounds_min[1],
                   bounds_max[1],
                   bounds_min[2],
                   bounds_max[2]]

    # Generate Grid Pts
    dx = data_bounds[1] - data_bounds[0]
    dy = data_bounds[3] - data_bounds[2]
    dz = data_bounds[5] - data_bounds[4]
    bounds = np.zeros(6)
    bounds[0] = data_bounds[0] - xy_buffer * dx
    bounds[1] = data_bounds[1] + xy_buffer * dx
    bounds[2] = data_bounds[2] - xy_buffer * dy
    bounds[3] = data_bounds[3] + xy_buffer * dy
    bounds[4] = data_bounds[4] - z_buffer * dz
    bounds[5] = data_bounds[5] + z_buffer * dz

    return bounds


def create_vtk_polydata_from_coords_and_property(coords: np.ndarray, prop: np.ndarray, prop_name: str):
    points_vtk = vtkPoints()
    vertices = vtkCellArray()
    for pt in coords:
        pt_id = points_vtk.InsertNextPoint(pt)
        vertices.InsertNextCell(1)
        vertices.InsertCellPoint(pt_id)
    poly = vtkPolyData()
    poly.SetPoints(points_vtk)
    poly.SetVerts(vertices)

    add_np_property_to_vtk_object(poly, prop_name, prop)
    return poly


class CustomLookUpTabel(object):
    def __init__(self, lut: vtkLookupTable):
        self.lut = lut

    def map_value(self, value):
        rgb = [0, 0, 0]
        self.lut.GetColor(value, rgb)
        return rgb


def CreateLUT(min, max):
    """
    Manual creation of the "Paired" color map. It maps property values (scalars) with the specified range (min, max)
    into RGB values. Scalar values outside of this range will be set to extremal RGB values in the color look up table.
    :param min: minimum scalar value
    :param max: maximum scalar value
    :return: a vtkLookupTable for a "Paired" color map within the scalar range of (min, max)
    """
    lut = vtkLookupTable()
    lut.SetNumberOfTableValues(256)
    lut.SetRange(min, max)
    lut.Build()
    ctf = vtkColorTransferFunction()
    ctf.SetColorSpaceToRGB()

    ctf.AddRGBPoint(0.000000, 0.650980, 0.807843, 0.890196)
    ctf.AddRGBPoint(0.003922, 0.628143, 0.793295, 0.882245)
    ctf.AddRGBPoint(0.007843, 0.605306, 0.778747, 0.874295)
    ctf.AddRGBPoint(0.011765, 0.582468, 0.764198, 0.866344)
    ctf.AddRGBPoint(0.015686, 0.559631, 0.749650, 0.858393)
    ctf.AddRGBPoint(0.019608, 0.536794, 0.735102, 0.850442)
    ctf.AddRGBPoint(0.023529, 0.513956, 0.720554, 0.842491)
    ctf.AddRGBPoint(0.027451, 0.491119, 0.706005, 0.834541)
    ctf.AddRGBPoint(0.031373, 0.468281, 0.691457, 0.826590)
    ctf.AddRGBPoint(0.035294, 0.445444, 0.676909, 0.818639)
    ctf.AddRGBPoint(0.039216, 0.422607, 0.662361, 0.810688)
    ctf.AddRGBPoint(0.043137, 0.399769, 0.647812, 0.802737)
    ctf.AddRGBPoint(0.047059, 0.376932, 0.633264, 0.794787)
    ctf.AddRGBPoint(0.050980, 0.354095, 0.618716, 0.786836)
    ctf.AddRGBPoint(0.054902, 0.331257, 0.604168, 0.778885)
    ctf.AddRGBPoint(0.058824, 0.308420, 0.589619, 0.770934)
    ctf.AddRGBPoint(0.062745, 0.285582, 0.575071, 0.762983)
    ctf.AddRGBPoint(0.066667, 0.262745, 0.560523, 0.755033)
    ctf.AddRGBPoint(0.070588, 0.239908, 0.545975, 0.747082)
    ctf.AddRGBPoint(0.074510, 0.217070, 0.531426, 0.739131)
    ctf.AddRGBPoint(0.078431, 0.194233, 0.516878, 0.731180)
    ctf.AddRGBPoint(0.082353, 0.171396, 0.502330, 0.723230)
    ctf.AddRGBPoint(0.086275, 0.148558, 0.487782, 0.715279)
    ctf.AddRGBPoint(0.090196, 0.125721, 0.473233, 0.707328)
    ctf.AddRGBPoint(0.094118, 0.141915, 0.484844, 0.700069)
    ctf.AddRGBPoint(0.098039, 0.166782, 0.502268, 0.692964)
    ctf.AddRGBPoint(0.101961, 0.191649, 0.519692, 0.685859)
    ctf.AddRGBPoint(0.105882, 0.216517, 0.537116, 0.678754)
    ctf.AddRGBPoint(0.109804, 0.241384, 0.554541, 0.671649)
    ctf.AddRGBPoint(0.113725, 0.266251, 0.571965, 0.664544)
    ctf.AddRGBPoint(0.117647, 0.291119, 0.589389, 0.657439)
    ctf.AddRGBPoint(0.121569, 0.315986, 0.606813, 0.650335)
    ctf.AddRGBPoint(0.125490, 0.340854, 0.624237, 0.643230)
    ctf.AddRGBPoint(0.129412, 0.365721, 0.641661, 0.636125)
    ctf.AddRGBPoint(0.133333, 0.390588, 0.659085, 0.629020)
    ctf.AddRGBPoint(0.137255, 0.415456, 0.676509, 0.621915)
    ctf.AddRGBPoint(0.141176, 0.440323, 0.693933, 0.614810)
    ctf.AddRGBPoint(0.145098, 0.465190, 0.711357, 0.607705)
    ctf.AddRGBPoint(0.149020, 0.490058, 0.728781, 0.600600)
    ctf.AddRGBPoint(0.152941, 0.514925, 0.746205, 0.593495)
    ctf.AddRGBPoint(0.156863, 0.539792, 0.763629, 0.586390)
    ctf.AddRGBPoint(0.160784, 0.564660, 0.781053, 0.579285)
    ctf.AddRGBPoint(0.164706, 0.589527, 0.798478, 0.572180)
    ctf.AddRGBPoint(0.168627, 0.614394, 0.815902, 0.565075)
    ctf.AddRGBPoint(0.172549, 0.639262, 0.833326, 0.557970)
    ctf.AddRGBPoint(0.176471, 0.664129, 0.850750, 0.550865)
    ctf.AddRGBPoint(0.180392, 0.688997, 0.868174, 0.543760)
    ctf.AddRGBPoint(0.184314, 0.684368, 0.867728, 0.531057)
    ctf.AddRGBPoint(0.188235, 0.662884, 0.857070, 0.515156)
    ctf.AddRGBPoint(0.192157, 0.641399, 0.846413, 0.499254)
    ctf.AddRGBPoint(0.196078, 0.619915, 0.835755, 0.483353)
    ctf.AddRGBPoint(0.200000, 0.598431, 0.825098, 0.467451)
    ctf.AddRGBPoint(0.203922, 0.576947, 0.814441, 0.451549)
    ctf.AddRGBPoint(0.207843, 0.555463, 0.803783, 0.435648)
    ctf.AddRGBPoint(0.211765, 0.533979, 0.793126, 0.419746)
    ctf.AddRGBPoint(0.215686, 0.512495, 0.782468, 0.403845)
    ctf.AddRGBPoint(0.219608, 0.491011, 0.771811, 0.387943)
    ctf.AddRGBPoint(0.223529, 0.469527, 0.761153, 0.372042)
    ctf.AddRGBPoint(0.227451, 0.448043, 0.750496, 0.356140)
    ctf.AddRGBPoint(0.231373, 0.426559, 0.739839, 0.340238)
    ctf.AddRGBPoint(0.235294, 0.405075, 0.729181, 0.324337)
    ctf.AddRGBPoint(0.239216, 0.383591, 0.718524, 0.308435)
    ctf.AddRGBPoint(0.243137, 0.362107, 0.707866, 0.292534)
    ctf.AddRGBPoint(0.247059, 0.340623, 0.697209, 0.276632)
    ctf.AddRGBPoint(0.250980, 0.319139, 0.686551, 0.260730)
    ctf.AddRGBPoint(0.254902, 0.297655, 0.675894, 0.244829)
    ctf.AddRGBPoint(0.258824, 0.276171, 0.665236, 0.228927)
    ctf.AddRGBPoint(0.262745, 0.254687, 0.654579, 0.213026)
    ctf.AddRGBPoint(0.266667, 0.233203, 0.643922, 0.197124)
    ctf.AddRGBPoint(0.270588, 0.211719, 0.633264, 0.181223)
    ctf.AddRGBPoint(0.274510, 0.215379, 0.626990, 0.180930)
    ctf.AddRGBPoint(0.278431, 0.249212, 0.625975, 0.199369)
    ctf.AddRGBPoint(0.282353, 0.283045, 0.624960, 0.217809)
    ctf.AddRGBPoint(0.286275, 0.316878, 0.623945, 0.236248)
    ctf.AddRGBPoint(0.290196, 0.350711, 0.622930, 0.254687)
    ctf.AddRGBPoint(0.294118, 0.384544, 0.621915, 0.273126)
    ctf.AddRGBPoint(0.298039, 0.418378, 0.620900, 0.291565)
    ctf.AddRGBPoint(0.301961, 0.452211, 0.619885, 0.310004)
    ctf.AddRGBPoint(0.305882, 0.486044, 0.618870, 0.328443)
    ctf.AddRGBPoint(0.309804, 0.519877, 0.617855, 0.346882)
    ctf.AddRGBPoint(0.313725, 0.553710, 0.616840, 0.365321)
    ctf.AddRGBPoint(0.317647, 0.587543, 0.615825, 0.383760)
    ctf.AddRGBPoint(0.321569, 0.621376, 0.614810, 0.402199)
    ctf.AddRGBPoint(0.325490, 0.655210, 0.613795, 0.420638)
    ctf.AddRGBPoint(0.329412, 0.689043, 0.612780, 0.439077)
    ctf.AddRGBPoint(0.333333, 0.722876, 0.611765, 0.457516)
    ctf.AddRGBPoint(0.337255, 0.756709, 0.610750, 0.475955)
    ctf.AddRGBPoint(0.341176, 0.790542, 0.609735, 0.494394)
    ctf.AddRGBPoint(0.345098, 0.824375, 0.608720, 0.512834)
    ctf.AddRGBPoint(0.349020, 0.858208, 0.607705, 0.531273)
    ctf.AddRGBPoint(0.352941, 0.892042, 0.606690, 0.549712)
    ctf.AddRGBPoint(0.356863, 0.925875, 0.605675, 0.568151)
    ctf.AddRGBPoint(0.360784, 0.959708, 0.604660, 0.586590)
    ctf.AddRGBPoint(0.364706, 0.983206, 0.598016, 0.594233)
    ctf.AddRGBPoint(0.368627, 0.979146, 0.576363, 0.573087)
    ctf.AddRGBPoint(0.372549, 0.975087, 0.554710, 0.551942)
    ctf.AddRGBPoint(0.376471, 0.971027, 0.533057, 0.530796)
    ctf.AddRGBPoint(0.380392, 0.966967, 0.511403, 0.509650)
    ctf.AddRGBPoint(0.384314, 0.962907, 0.489750, 0.488504)
    ctf.AddRGBPoint(0.388235, 0.958847, 0.468097, 0.467359)
    ctf.AddRGBPoint(0.392157, 0.954787, 0.446444, 0.446213)
    ctf.AddRGBPoint(0.396078, 0.950727, 0.424790, 0.425067)
    ctf.AddRGBPoint(0.400000, 0.946667, 0.403137, 0.403922)
    ctf.AddRGBPoint(0.403922, 0.942607, 0.381484, 0.382776)
    ctf.AddRGBPoint(0.407843, 0.938547, 0.359831, 0.361630)
    ctf.AddRGBPoint(0.411765, 0.934487, 0.338178, 0.340484)
    ctf.AddRGBPoint(0.415686, 0.930427, 0.316524, 0.319339)
    ctf.AddRGBPoint(0.419608, 0.926367, 0.294871, 0.298193)
    ctf.AddRGBPoint(0.423529, 0.922307, 0.273218, 0.277047)
    ctf.AddRGBPoint(0.427451, 0.918247, 0.251565, 0.255902)
    ctf.AddRGBPoint(0.431373, 0.914187, 0.229912, 0.234756)
    ctf.AddRGBPoint(0.435294, 0.910127, 0.208258, 0.213610)
    ctf.AddRGBPoint(0.439216, 0.906067, 0.186605, 0.192464)
    ctf.AddRGBPoint(0.443137, 0.902007, 0.164952, 0.171319)
    ctf.AddRGBPoint(0.447059, 0.897947, 0.143299, 0.150173)
    ctf.AddRGBPoint(0.450980, 0.893887, 0.121646, 0.129027)
    ctf.AddRGBPoint(0.454902, 0.890596, 0.104498, 0.111080)
    ctf.AddRGBPoint(0.458824, 0.894994, 0.132411, 0.125121)
    ctf.AddRGBPoint(0.462745, 0.899393, 0.160323, 0.139162)
    ctf.AddRGBPoint(0.466667, 0.903791, 0.188235, 0.153203)
    ctf.AddRGBPoint(0.470588, 0.908189, 0.216148, 0.167243)
    ctf.AddRGBPoint(0.474510, 0.912587, 0.244060, 0.181284)
    ctf.AddRGBPoint(0.478431, 0.916986, 0.271972, 0.195325)
    ctf.AddRGBPoint(0.482353, 0.921384, 0.299885, 0.209366)
    ctf.AddRGBPoint(0.486275, 0.925782, 0.327797, 0.223406)
    ctf.AddRGBPoint(0.490196, 0.930181, 0.355709, 0.237447)
    ctf.AddRGBPoint(0.494118, 0.934579, 0.383622, 0.251488)
    ctf.AddRGBPoint(0.498039, 0.938977, 0.411534, 0.265529)
    ctf.AddRGBPoint(0.501961, 0.943376, 0.439446, 0.279569)
    ctf.AddRGBPoint(0.505882, 0.947774, 0.467359, 0.293610)
    ctf.AddRGBPoint(0.509804, 0.952172, 0.495271, 0.307651)
    ctf.AddRGBPoint(0.513725, 0.956571, 0.523183, 0.321692)
    ctf.AddRGBPoint(0.517647, 0.960969, 0.551096, 0.335732)
    ctf.AddRGBPoint(0.521569, 0.965367, 0.579008, 0.349773)
    ctf.AddRGBPoint(0.525490, 0.969765, 0.606920, 0.363814)
    ctf.AddRGBPoint(0.529412, 0.974164, 0.634833, 0.377855)
    ctf.AddRGBPoint(0.533333, 0.978562, 0.662745, 0.391895)
    ctf.AddRGBPoint(0.537255, 0.982960, 0.690657, 0.405936)
    ctf.AddRGBPoint(0.541176, 0.987359, 0.718570, 0.419977)
    ctf.AddRGBPoint(0.545098, 0.991757, 0.746482, 0.434018)
    ctf.AddRGBPoint(0.549020, 0.992464, 0.739177, 0.418224)
    ctf.AddRGBPoint(0.552941, 0.992803, 0.728351, 0.399446)
    ctf.AddRGBPoint(0.556863, 0.993141, 0.717524, 0.380669)
    ctf.AddRGBPoint(0.560784, 0.993479, 0.706697, 0.361892)
    ctf.AddRGBPoint(0.564706, 0.993818, 0.695871, 0.343114)
    ctf.AddRGBPoint(0.568627, 0.994156, 0.685044, 0.324337)
    ctf.AddRGBPoint(0.572549, 0.994494, 0.674218, 0.305559)
    ctf.AddRGBPoint(0.576471, 0.994833, 0.663391, 0.286782)
    ctf.AddRGBPoint(0.580392, 0.995171, 0.652564, 0.268005)
    ctf.AddRGBPoint(0.584314, 0.995509, 0.641738, 0.249227)
    ctf.AddRGBPoint(0.588235, 0.995848, 0.630911, 0.230450)
    ctf.AddRGBPoint(0.592157, 0.996186, 0.620085, 0.211672)
    ctf.AddRGBPoint(0.596078, 0.996524, 0.609258, 0.192895)
    ctf.AddRGBPoint(0.600000, 0.996863, 0.598431, 0.174118)
    ctf.AddRGBPoint(0.603922, 0.997201, 0.587605, 0.155340)
    ctf.AddRGBPoint(0.607843, 0.997539, 0.576778, 0.136563)
    ctf.AddRGBPoint(0.611765, 0.997878, 0.565952, 0.117785)
    ctf.AddRGBPoint(0.615686, 0.998216, 0.555125, 0.099008)
    ctf.AddRGBPoint(0.619608, 0.998554, 0.544298, 0.080231)
    ctf.AddRGBPoint(0.623529, 0.998893, 0.533472, 0.061453)
    ctf.AddRGBPoint(0.627451, 0.999231, 0.522645, 0.042676)
    ctf.AddRGBPoint(0.631373, 0.999569, 0.511819, 0.023899)
    ctf.AddRGBPoint(0.635294, 0.999908, 0.500992, 0.005121)
    ctf.AddRGBPoint(0.639216, 0.993479, 0.504314, 0.026328)
    ctf.AddRGBPoint(0.643137, 0.984514, 0.512941, 0.062530)
    ctf.AddRGBPoint(0.647059, 0.975548, 0.521569, 0.098731)
    ctf.AddRGBPoint(0.650980, 0.966582, 0.530196, 0.134933)
    ctf.AddRGBPoint(0.654902, 0.957616, 0.538824, 0.171134)
    ctf.AddRGBPoint(0.658824, 0.948651, 0.547451, 0.207336)
    ctf.AddRGBPoint(0.662745, 0.939685, 0.556078, 0.243537)
    ctf.AddRGBPoint(0.666667, 0.930719, 0.564706, 0.279739)
    ctf.AddRGBPoint(0.670588, 0.921753, 0.573333, 0.315940)
    ctf.AddRGBPoint(0.674510, 0.912787, 0.581961, 0.352141)
    ctf.AddRGBPoint(0.678431, 0.903822, 0.590588, 0.388343)
    ctf.AddRGBPoint(0.682353, 0.894856, 0.599216, 0.424544)
    ctf.AddRGBPoint(0.686275, 0.885890, 0.607843, 0.460746)
    ctf.AddRGBPoint(0.690196, 0.876924, 0.616471, 0.496947)
    ctf.AddRGBPoint(0.694118, 0.867958, 0.625098, 0.533149)
    ctf.AddRGBPoint(0.698039, 0.858993, 0.633726, 0.569350)
    ctf.AddRGBPoint(0.701961, 0.850027, 0.642353, 0.605552)
    ctf.AddRGBPoint(0.705882, 0.841061, 0.650980, 0.641753)
    ctf.AddRGBPoint(0.709804, 0.832095, 0.659608, 0.677955)
    ctf.AddRGBPoint(0.713725, 0.823130, 0.668235, 0.714156)
    ctf.AddRGBPoint(0.717647, 0.814164, 0.676863, 0.750358)
    ctf.AddRGBPoint(0.721569, 0.805198, 0.685490, 0.786559)
    ctf.AddRGBPoint(0.725490, 0.796232, 0.694118, 0.822760)
    ctf.AddRGBPoint(0.729412, 0.783299, 0.687243, 0.833679)
    ctf.AddRGBPoint(0.733333, 0.767059, 0.667451, 0.823529)
    ctf.AddRGBPoint(0.737255, 0.750819, 0.647659, 0.813379)
    ctf.AddRGBPoint(0.741176, 0.734579, 0.627866, 0.803230)
    ctf.AddRGBPoint(0.745098, 0.718339, 0.608074, 0.793080)
    ctf.AddRGBPoint(0.749020, 0.702099, 0.588281, 0.782930)
    ctf.AddRGBPoint(0.752941, 0.685859, 0.568489, 0.772780)
    ctf.AddRGBPoint(0.756863, 0.669619, 0.548697, 0.762630)
    ctf.AddRGBPoint(0.760784, 0.653379, 0.528904, 0.752480)
    ctf.AddRGBPoint(0.764706, 0.637140, 0.509112, 0.742330)
    ctf.AddRGBPoint(0.768627, 0.620900, 0.489320, 0.732180)
    ctf.AddRGBPoint(0.772549, 0.604660, 0.469527, 0.722030)
    ctf.AddRGBPoint(0.776471, 0.588420, 0.449735, 0.711880)
    ctf.AddRGBPoint(0.780392, 0.572180, 0.429942, 0.701730)
    ctf.AddRGBPoint(0.784314, 0.555940, 0.410150, 0.691580)
    ctf.AddRGBPoint(0.788235, 0.539700, 0.390358, 0.681430)
    ctf.AddRGBPoint(0.792157, 0.523460, 0.370565, 0.671280)
    ctf.AddRGBPoint(0.796078, 0.507220, 0.350773, 0.661130)
    ctf.AddRGBPoint(0.800000, 0.490980, 0.330980, 0.650980)
    ctf.AddRGBPoint(0.803922, 0.474740, 0.311188, 0.640830)
    ctf.AddRGBPoint(0.807843, 0.458501, 0.291396, 0.630681)
    ctf.AddRGBPoint(0.811765, 0.442261, 0.271603, 0.620531)
    ctf.AddRGBPoint(0.815686, 0.426021, 0.251811, 0.610381)
    ctf.AddRGBPoint(0.819608, 0.424852, 0.251150, 0.603860)
    ctf.AddRGBPoint(0.823529, 0.450058, 0.283968, 0.603691)
    ctf.AddRGBPoint(0.827451, 0.475263, 0.316786, 0.603522)
    ctf.AddRGBPoint(0.831373, 0.500469, 0.349604, 0.603353)
    ctf.AddRGBPoint(0.835294, 0.525675, 0.382422, 0.603183)
    ctf.AddRGBPoint(0.839216, 0.550880, 0.415240, 0.603014)
    ctf.AddRGBPoint(0.843137, 0.576086, 0.448058, 0.602845)
    ctf.AddRGBPoint(0.847059, 0.601292, 0.480877, 0.602676)
    ctf.AddRGBPoint(0.850980, 0.626498, 0.513695, 0.602507)
    ctf.AddRGBPoint(0.854902, 0.651703, 0.546513, 0.602338)
    ctf.AddRGBPoint(0.858824, 0.676909, 0.579331, 0.602168)
    ctf.AddRGBPoint(0.862745, 0.702115, 0.612149, 0.601999)
    ctf.AddRGBPoint(0.866667, 0.727320, 0.644967, 0.601830)
    ctf.AddRGBPoint(0.870588, 0.752526, 0.677785, 0.601661)
    ctf.AddRGBPoint(0.874510, 0.777732, 0.710604, 0.601492)
    ctf.AddRGBPoint(0.878431, 0.802937, 0.743422, 0.601323)
    ctf.AddRGBPoint(0.882353, 0.828143, 0.776240, 0.601153)
    ctf.AddRGBPoint(0.886275, 0.853349, 0.809058, 0.600984)
    ctf.AddRGBPoint(0.890196, 0.878554, 0.841876, 0.600815)
    ctf.AddRGBPoint(0.894118, 0.903760, 0.874694, 0.600646)
    ctf.AddRGBPoint(0.898039, 0.928966, 0.907512, 0.600477)
    ctf.AddRGBPoint(0.901961, 0.954171, 0.940331, 0.600308)
    ctf.AddRGBPoint(0.905882, 0.979377, 0.973149, 0.600138)
    ctf.AddRGBPoint(0.909804, 0.997601, 0.994894, 0.596524)
    ctf.AddRGBPoint(0.913725, 0.984406, 0.966813, 0.577409)
    ctf.AddRGBPoint(0.917647, 0.971211, 0.938731, 0.558293)
    ctf.AddRGBPoint(0.921569, 0.958016, 0.910650, 0.539177)
    ctf.AddRGBPoint(0.925490, 0.944821, 0.882568, 0.520062)
    ctf.AddRGBPoint(0.929412, 0.931626, 0.854487, 0.500946)
    ctf.AddRGBPoint(0.933333, 0.918431, 0.826405, 0.481830)
    ctf.AddRGBPoint(0.937255, 0.905236, 0.798324, 0.462714)
    ctf.AddRGBPoint(0.941176, 0.892042, 0.770242, 0.443599)
    ctf.AddRGBPoint(0.945098, 0.878847, 0.742161, 0.424483)
    ctf.AddRGBPoint(0.949020, 0.865652, 0.714079, 0.405367)
    ctf.AddRGBPoint(0.952941, 0.852457, 0.685998, 0.386251)
    ctf.AddRGBPoint(0.956863, 0.839262, 0.657916, 0.367136)
    ctf.AddRGBPoint(0.960784, 0.826067, 0.629835, 0.348020)
    ctf.AddRGBPoint(0.964706, 0.812872, 0.601753, 0.328904)
    ctf.AddRGBPoint(0.968627, 0.799677, 0.573672, 0.309789)
    ctf.AddRGBPoint(0.972549, 0.786482, 0.545590, 0.290673)
    ctf.AddRGBPoint(0.976471, 0.773287, 0.517509, 0.271557)
    ctf.AddRGBPoint(0.980392, 0.760092, 0.489427, 0.252441)
    ctf.AddRGBPoint(0.984314, 0.746897, 0.461346, 0.233326)
    ctf.AddRGBPoint(0.988235, 0.733702, 0.433264, 0.214210)
    ctf.AddRGBPoint(0.992157, 0.720508, 0.405183, 0.195094)
    ctf.AddRGBPoint(0.996078, 0.707313, 0.377101, 0.175978)
    ctf.AddRGBPoint(1.000000, 0.694118, 0.349020, 0.156863)

    for i in range(256):
        r, g, b = ctf.GetColor(float(i / 255.0))
        lut.SetTableValue(i, r, g, b)
    return CustomLookUpTabel(lut)


def write_slices_from_unstructured_grid_as_pictures(grid: vtkUnstructuredGrid, output_dir):
    """
    This function will obtain 9 slices of the vtkUnstructuredGrid (3 per axis (x, y, z) - left, middle, right) to
    visualize the 1-component scalar array (vtkDoubleArray) it's attributed with. The slices will be converted into
    images using a specified color map.
    In order to achieve this objective a slice represented by a vtkImageData is used to probe the vtkUnstructuredGrid
    where the scalar property will be interpolated from.
    For non-z-slices vtkImageData will have to be re-orientated to create a proper XY image from. vtkImagePermute class
    is used for this purpose.
    :param grid: vtkUnstructuredGrid containing a 1-component scalar array (vtkDoubleArray)
    :param output_dir: output directory where png images will be written to.
    :return: nothing
    """

    # resolution of outputted images: pixel's spatial resolution
    spacing = 0.5

    # get the value range of the scalar data
    value_array = grid.GetPointData().GetArray("Scalar Field")
    vmin, vmax = value_array.GetRange()

    # create "Paired" color map with the given range
    lut = CreateLUT(vmin, vmax)
    xmin, xmax, ymin, ymax, zmin, zmax = grid.GetBounds()

    #  specify x/y/z positions of slices
    #  3 per direction : {left, middle, right}
    slice_pos = []
    # positions of x-slices
    slice_pos.append(xmin)
    slice_pos.append(xmin + ((xmax - xmin) / 2.0))
    slice_pos.append(xmax)

    # positions of y-slices
    slice_pos.append(ymin)
    slice_pos.append(ymin + ((ymax - ymin) / 2.0))
    slice_pos.append(ymax)

    # positions of z-slices
    slice_pos.append(zmin)
    slice_pos.append(zmin + ((zmax - zmin) / 2.0))
    slice_pos.append(zmax)

    for i, pos in enumerate(slice_pos):
        slice = vtkImageData()
        slice.SetSpacing(spacing, spacing, spacing)
        # x-slices
        if i < 3:
            tag = "x-slice_" + str(i)
            s_xmin = pos
            s_xmax = pos
            s_ymin = ymin
            s_ymax = ymax
            s_zmin = zmin
            s_zmax = zmax
        # y-slices
        if 2 < i < 6:
            tag = "y-slice_" + str(i)
            s_xmin = xmin
            s_xmax = xmax
            s_ymin = pos
            s_ymax = pos
            s_zmin = zmin
            s_zmax = zmax
        # z-slices
        if i > 5:
            tag = "z-slice_" + str(i)
            s_xmin = xmin
            s_xmax = xmax
            s_ymin = ymin
            s_ymax = ymax
            s_zmin = pos
            s_zmax = pos
        dim_x = int(math.ceil((s_xmax - s_xmin) / spacing))
        dim_y = int(math.ceil((s_ymax - s_ymin) / spacing))
        dim_z = int(math.ceil((s_zmax - s_zmin) / spacing))

        # x-slices
        if i < 3:
            extent_x = 0
            extent_y = dim_y - 1
            extent_z = dim_z - 1
        # y-slices
        if 2 < i < 6:
            extent_x = dim_x - 1
            extent_y = 0
            extent_z = dim_z - 1
        # z-slices
        if i > 5:
            extent_x = dim_x - 1
            extent_y = dim_y - 1
            extent_z = 0
        slice.SetExtent(0, extent_x, 0, extent_y, 0, extent_z)
        slice.SetOrigin(s_xmin, s_ymin, s_zmin)

        probe = vtkProbeFilter()
        probe.SetInputData(slice)
        probe.SetSourceData(grid)
        probe.Update()

        out = probe.GetOutput()
        arr = out.GetPointData().GetArray("Scalar Field")
        # set scalars to this array - needed for vtkImageMapToColors filter
        # needs to know which scalar array to use to map scalars to colors
        out.GetPointData().SetScalars(arr)

        # probe_writer = vtkXMLImageDataWriter()
        # img_filename = "D:/image_slice_probe_python_" + str(i) + ".vti"
        # probe_writer.SetFileName(img_filename)
        # probe_writer.SetInputData(out)
        # probe_writer.Write()

        if i < 6:
            # for x and y slices images orientations have to be switched so that the png writer
            # will correctly export the image - images must have x and y orientation
            # z slices are already oriented in x and y
            permute = vtkImagePermute()
            permute.SetInputData(out)
            # x-slice
            if i < 3:
                # z direction will become x direction
                permute.SetFilteredAxes(2, 1, 0)
            # y-slice
            if i > 2:
                # z direction will become y direction
                permute.SetFilteredAxes(0, 2, 1)
            permute.Update()

            permute_img = permute.GetOutput()
            perm_arr = permute_img.GetPointData().GetArray(0)
            # set scalars to this array - needed for vtkImageMapToColors filter
            # needs to know which scalar array to use to map scalars to colors
            permute_img.GetPointData().SetScalars(perm_arr)

            # perm_writer = vtkXMLImageDataWriter()
            # perm_filename = "D:/image_slice_permute_python_" + str(i) + ".vti"
            # perm_writer.SetFileName(perm_filename)
            # perm_writer.SetInputData(permute_img)
            # perm_writer.Write()

            scalarValuesToColors = vtkImageMapToColors()
            scalarValuesToColors.SetLookupTable(lut)
            scalarValuesToColors.SetOutputFormatToRGB()
            scalarValuesToColors.SetInputData(permute_img)
            scalarValuesToColors.Update()
        else:
            scalarValuesToColors = vtkImageMapToColors()
            scalarValuesToColors.SetLookupTable(lut)
            scalarValuesToColors.SetOutputFormatToRGB()
            scalarValuesToColors.SetInputData(out)
            scalarValuesToColors.Update()

        png_writer = vtkPNGWriter()
        png_filename = output_dir + "/image_slice_" + str(i) + ".png"
        png_writer.SetFileName(png_filename)
        png_writer.SetInputData(scalarValuesToColors.GetOutput())
        png_writer.Write()


def reader_xml_polydata_file(pd_filename: str):
    """
    :param pd_filename: an XML vtkPolyData file format (*.vtp)
    :return: vtkPolyData data structure - also carries any property data attributed to points, cells, as well as field
             data (meta data)
    """
    if not os.path.isfile(pd_filename):
        raise ValueError('File does not exist')

    reader = vtk.vtkXMLPolyDataReader()
    reader.SetFileName(pd_filename)
    reader.Update()

    return reader.GetOutput()


def reader_unstructured_mesh_file(mesh_filename: str):
    '''
    :param mesh_filename: vtk legacy file format representing a vtkUnstructuredGrid
    :return: a vtkUnstructuredGrid (just geometry and topology - no property)
    '''
    if not os.path.isfile(mesh_filename):
        raise ValueError('File does not exist')

    filename, file_extension = os.path.splitext(mesh_filename)
    if file_extension == '.vtk':
        reader = vtk.vtkUnstructuredGridReader()
    else:
        reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(mesh_filename)
    reader.Update()

    return reader.GetOutput()

##
# def add_terrain_to_base_grid(terrain, base_grid):
#     grid_bounds = base_grid.bounds
#     terrain.extend_mesh_from_surface_by_bounds(bounds=grid_bounds)
#     vol = vtk_unstructured_grid_to_vtk_polydata(terrain.vtk_data)
#     vol = vol.triangulate()
#     base_grid = vtk_unstructured_grid_to_vtk_polydata(base_grid)
#     base_grid = base_grid.triangulate()
#     intersect = base_grid.boolean_intersection(vol)
#     intersect.plot()
#     other = base_grid.boolean_difference(intersect)
#     other.plot()
