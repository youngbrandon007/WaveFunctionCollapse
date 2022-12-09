
from copy import deepcopy
from queue import PriorityQueue

import sys
from collections import defaultdict
import random

import numpy as np
import pycuda.autoinit
import pycuda.driver as drv
from pycuda.compiler import SourceModule

from gpu_helper import createsBlockGridSizes

sys.setrecursionlimit(10000)


# Create a CUDA kernel
compute_entropy_module = SourceModule("""
__global__ void compute_entropy(bool *wave, unsigned int *OUTPUT_X, unsigned int *OUTPUT_Y, unsigned int *tile_count, unsigned int *entropy) {
	const int x = threadIdx.x + blockIdx.x * blockDim.x;
	const int y = threadIdx.y + blockIdx.y * blockDim.y;
	const int chunk = x + y * *OUTPUT_X;

	if(x >= *OUTPUT_X || y >= *OUTPUT_Y) return;

	int t, score = 0;
	for(t = 0; t < *tile_count; ++t){
		score += wave[chunk * *tile_count + t];
	}

	entropy[chunk] = score;
}
""")
compute_entropy = compute_entropy_module.get_function("compute_entropy")


compute_lowest_col_entropy_module = SourceModule("""
__global__ void compute_lowest_col_entropy(unsigned int *entropy, unsigned int *OUTPUT_X, unsigned int *OUTPUT_Y, unsigned int *tile_count, unsigned int *count_in_cols, unsigned int *lowest_value_in_cols, bool *solve_state) {
	const int x = threadIdx.x + blockIdx.x * blockDim.x;

	if(x >= *OUTPUT_X) return;

	int y, lowest_value=*tile_count+1, counter=0, chunk, temp;
	for(y = 0; y < *OUTPUT_Y; ++y){
		chunk = x + y * *OUTPUT_X;
		temp = entropy[chunk];
		
		if(temp == 0) {
			solve_state[1] = 1;
		}else if(temp != 1){
			solve_state[0] = 0;
			if(temp < lowest_value){
				lowest_value = temp;
				counter = 0;
			}
		}

		if(lowest_value == temp){
			++counter;
		}
	}

	count_in_cols[x] = counter;
	lowest_value_in_cols[x] = lowest_value;
}
""")
compute_lowest_col_entropy = compute_lowest_col_entropy_module.get_function("compute_lowest_col_entropy")



compute_entropy_position_module = SourceModule("""
__global__ void compute_entropy_position(bool *wave, unsigned int *entropy, unsigned int *OUTPUT_X, unsigned int *OUTPUT_Y, unsigned int *tile_count, float *rand1, float *rand2, unsigned int *count_in_cols, unsigned int *lowest_value_in_cols, unsigned int *entropy_position) {
	int x, smallest_value = (*tile_count + 1), counter=0, col_smallest_value, col_counter;
	for(x = 0; x < *OUTPUT_X; ++x){
		col_smallest_value = lowest_value_in_cols[x];

		if(col_smallest_value < smallest_value){
			smallest_value = col_smallest_value;
			counter = 0;
		}

		if(smallest_value == col_smallest_value){
			counter += count_in_cols[x];
		}
	}

	int index = (int) (*rand1 * counter);

	for(x = 0; x < *OUTPUT_X; ++x){
		col_counter = count_in_cols[x];

		if(index >= col_counter){
			index -= col_counter;
		}else{
			break;
		}
	}

	// X value is now found
	int temp, chunk, y;
	for(y = 0; y < *OUTPUT_Y; ++y){
		chunk = x + y * *OUTPUT_X;
		temp = entropy[chunk];

		if(temp == smallest_value){
			if(index == 0){
				break;
			}
			index -= 1;
		}
	}

	// X and Y are both found and chunk var. is filled in
	
	entropy_position[0] = x;
	entropy_position[1] = y;

	int tile_index = smallest_value * *rand2;

	int t, val;
	for(t = 0; t < *tile_count; ++t){
		if(wave[chunk * *tile_count + t]){
			wave[chunk * *tile_count + t] = (tile_index == 0);
			tile_index -= 1;
		}
	}
}
""")
compute_entropy_position = compute_entropy_position_module.get_function("compute_entropy_position")


# referenceGlobal, N, ROTATION, MIRRORING_HORZ, MIRRORING_VERT, OUTPUT_X, OUTPUT_Y
def gen(referenceGlobal, IS_input:str, N_input:int, R:bool, MH:bool, MV:bool, OUTPUT_X:int, OUTPUT_Y:int, c_input:list[tuple[int,int,int,int]]):

	colors_array = np.array(c_input, dtype=np.uint8)
	N = np.uint8(N_input)

	out_x = np.uint32(OUTPUT_X)
	out_y = np.uint32(OUTPUT_Y)
		
	def createTiles(input_str, ROTATION, MIRRORING_HORZ, MIRRORING_VERT):
		def inputStrToList(data:list[str]) -> list[list[int]]:
			# Convert the input string data to a list of lists of integers
			return [list(i) for i in data.strip().split("\n")]
		
		def rotate90Clockwise(A):
			# Get the length of one side of the square
			N = len(A[0])
			# Loop through the top half of the square
			for i in range(N // 2):
				for j in range(i, N - i - 1):
					# Store the element at the top left corner of the sub-square
					temp = A[i][j]
					# Swap the elements at the four corners of the sub-square
					A[i][j] = A[N - 1 - j][i]
					A[N - 1 - j][i] = A[N - 1 - i][N - 1 - j]
					A[N - 1 - i][N - 1 - j] = A[j][N - 1 - i]
					A[j][N - 1 - i] = temp
			# The square A has been rotated clockwise by 90 degrees
			return A

		def mirrorHorz(A):
			# Calculate the length of the first row of the input array
			l = len(A[0])
			# Loop through half of the array, from 0 to (l // 2) - 1
			for x in range(l // 2):
				# Loop through all the rows in the array
				for y in range(len(A)):
					# Swap the elements at the x and l-x-1 indices of the y-th row of A
					A[y][x], A[y][l-x-1] = A[y][l-x-1], A[y][x]

		def mirrorVert(A):
			# Calculate the length of the input array
			l = len(A)
			# Loop through half of the array, from 0 to (l // 2) - 1
			for y in range(l // 2):
				# Loop through all the columns in the array
				for x in range(len(A[0])):
					# Swap the elements at the y and l-y-1 indices of the x-th column of A
					A[y][x], A[l-y-1][x] = A[l-y-1][x], A[y][x]

		# tile must be square
		def createRotations(tile:tuple[tuple[int]]) -> list[tuple[tuple[int]]]:
			output = []
			arr = list(list(i) for i in tile)
			for _ in range(4):
				output.append(tuple(tuple(i) for i in arr))
				if MIRRORING_HORZ:
					mirrorHorz(arr)
					output.append(tuple(tuple(i) for i in arr))
				if MIRRORING_VERT:
					mirrorVert(arr)
					output.append(tuple(tuple(i) for i in arr))
				if MIRRORING_HORZ:
					mirrorHorz(arr)
					output.append(tuple(tuple(i) for i in arr))
				if MIRRORING_VERT:
					mirrorVert(arr)

				if not ROTATION: break
				rotate90Clockwise(arr)

			return output
		
		input_map = inputStrToList(input_str)
		input_width = len(input_map[0])
		input_height = len(input_map)

		tiles = defaultdict(lambda: 0)

		for x in range(input_width - N + 1):
			for y in range(input_height - N + 1):
				t = tuple(tuple(k for k in i[x:x+N]) for i in input_map[y:y+N])
				for i in createRotations(t):
					# TODO reduce count for rotation and mirroring
					tiles[i] += 1
		
		temp_tiles_array = []
		temp_tiles_array_counts = []
		for k,v in tiles.items():
			temp_tiles_array.append(k)
			temp_tiles_array_counts.append(v)
		
		tile_array = np.array(temp_tiles_array, np.uint8)
		tile_array_counts = np.array(temp_tiles_array_counts, dtype=np.uint32)
		
		# print(tiles_array)
		# print(tiles_array_counts)

		print(f"Generated {len(tile_array)} Tiles")

		return tile_array, tile_array_counts

	tile_array, tile_array_counts = createTiles(IS_input, R, MH, MV)

	tile_count = np.uint32(len(tile_array))

	wave = np.ones((OUTPUT_Y, OUTPUT_X, tile_count), dtype=bool)
	wave_output= np.ones((OUTPUT_Y, OUTPUT_X, tile_count), dtype=bool)

	entropy_array = np.ones((OUTPUT_Y, OUTPUT_X), dtype=np.uint32)

	count_in_cols = np.zeros(OUTPUT_X, dtype=np.uint32)
	lowest_value_in_cols = np.zeros(OUTPUT_X, dtype=np.uint32)
	
	solve_state = np.zeros(2, dtype=bool)	
	# Default Win to true
	solve_state[0] = 1

	entropy_position = np.zeros(2, dtype=np.uint32)	

	# GPU Memory Definitions
	wave_gpu = drv.mem_alloc(wave.nbytes)
	drv.memcpy_htod(wave_gpu, wave)

	entropy_array_gpu = drv.mem_alloc(entropy_array.nbytes)
	drv.memcpy_htod(entropy_array_gpu, entropy_array)

	count_in_cols_gpu = drv.mem_alloc(count_in_cols.nbytes)
	drv.memcpy_htod(count_in_cols_gpu, count_in_cols)

	lowest_value_in_cols_gpu = drv.mem_alloc(lowest_value_in_cols.nbytes)
	drv.memcpy_htod(lowest_value_in_cols_gpu, lowest_value_in_cols)

	# win_bool_gpu = drv.mem_alloc(win_bool.nbytes)
	# drv.memcpy_htod(win_bool_gpu, win_bool)

	# fail_bool_gpu = drv.mem_alloc(fail_bool.nbytes)
	# drv.memcpy_htod(fail_bool_gpu, fail_bool)
	
	####################|FINALS|######################
	out_x_gpu = drv.mem_alloc(out_x.nbytes)
	drv.memcpy_htod(out_x_gpu, out_x)
	
	out_y_gpu = drv.mem_alloc(out_y.nbytes)
	drv.memcpy_htod(out_y_gpu, out_y)
	
	N_gpu = drv.mem_alloc(N.nbytes)
	drv.memcpy_htod(N_gpu, N)

	tile_count_gpu = drv.mem_alloc(tile_count.nbytes)
	drv.memcpy_htod(tile_count_gpu, tile_count)

	tile_array_gpu = drv.mem_alloc(tile_array.nbytes)
	drv.memcpy_htod(tile_array_gpu, tile_array)
	##################################################
	
	referenceGlobal[:] = [wave_output, N, tile_count, tile_array, colors_array]

	def saveWave():
		drv.memcpy_dtoh(wave_output, wave_gpu)
		referenceGlobal[0] = wave_output

	saveWave()

	def solve():
		# Generate entropies
		block, grid = createsBlockGridSizes(OUTPUT_X, OUTPUT_Y, 1)
		compute_entropy(wave_gpu, out_x_gpu, out_y_gpu, tile_count_gpu, entropy_array_gpu, block=block, grid=grid)

		# Calculate Entropty Columns and Fail/Success states
		block, grid = createsBlockGridSizes(OUTPUT_X, 1, 1)
		compute_lowest_col_entropy(entropy_array_gpu, out_x_gpu, out_y_gpu, tile_count_gpu, count_in_cols_gpu, lowest_value_in_cols_gpu, drv.InOut(solve_state), block=block, grid=grid)

		win_bool, fail_bool = solve_state
		
		if win_bool: return True
		if fail_bool: return False

		block, grid = createsBlockGridSizes(1, 1, 1)
		
		rand1 = np.float32(np.random.rand())
		rand2 = np.float32(np.random.rand())

		compute_entropy_position(wave_gpu, entropy_array_gpu, out_x_gpu, out_y_gpu, tile_count_gpu, drv.In(rand1), drv.In(rand2), count_in_cols_gpu, lowest_value_in_cols_gpu, drv.InOut(entropy_position), block=block, grid=grid)

		print("Random 1:", rand1)
		print("Random 2:", rand2)
		print(f"Chosen {entropy_position[0], entropy_position[1]}")

		
		drv.memcpy_dtoh(wave, wave_gpu)

		# print(wave)

		saveWave()
		# drv.memcpy_dtoh(count_in_cols, count_in_cols_gpu)
		# drv.memcpy_dtoh(lowest_value_in_cols, lowest_value_in_cols_gpu)

		# print(entropy_array)
	
		

	solve()