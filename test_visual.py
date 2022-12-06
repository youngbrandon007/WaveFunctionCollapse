import numpy as np
import pycuda.autoinit
import pycuda.driver as drv
from pycuda.compiler import SourceModule
import time

from run import TileLocation

def compute(OUTPUT_X, OUTPUT_Y, wave_input:list[list[TileLocation]], N_input, COLORS_input, tile_type_from_id_input, block_type_from_id_input):
# Set up the data
	tile_count = np.uint32(len(tile_type_from_id_input))

	# wave = np.ones((OUTPUT_X,OUTPUT_Y,tile_count), dtype=bool)

	wave = np.array([[i.tiles for i in row] for row in wave_input], dtype=bool)

	N = np.uint8(N_input)

	tile_array = np.array([v for k, v in sorted(tile_type_from_id_input.items())], dtype=np.uint8)

	colors_array = np.array([COLORS_input[block_type_from_id_input[i]] for i in sorted(block_type_from_id_input)], dtype=np.uint8)

	output = np.zeros((OUTPUT_X, OUTPUT_Y, 4), dtype=np.uint32)

	# Transfer the data to the GPU

	wave_gpu = drv.mem_alloc(wave.nbytes)
	drv.memcpy_htod(wave_gpu, wave)

	N_gpu = drv.mem_alloc(N.nbytes)
	drv.memcpy_htod(N_gpu, N)

	tile_count_gpu = drv.mem_alloc(tile_count.nbytes)
	drv.memcpy_htod(tile_count_gpu, tile_count)

	tile_array_gpu = drv.mem_alloc(tile_array.nbytes)
	drv.memcpy_htod(tile_array_gpu, tile_array)

	colors_array_gpu = drv.mem_alloc(colors_array.nbytes)
	drv.memcpy_htod(colors_array_gpu, colors_array)

	output_gpu = drv.mem_alloc(output.nbytes)

	# Create a CUDA kernel
	mod = SourceModule("""
		__global__ void compute_colors(bool *wave, unsigned char *N, unsigned int *tile_count, unsigned char *tile_array, unsigned char *colors_array, unsigned int *output)
		{
			const int x = threadIdx.x + blockIdx.x * blockDim.x;
			const int y = threadIdx.y + blockIdx.y * blockDim.y;
			const int chunk = x + y * blockDim.x * gridDim.x;

			output[4 * chunk + 0] = 0;
			output[4 * chunk + 1] = 0;
			output[4 * chunk + 2] = 0;
			output[4 * chunk + 3] = 0;
			
			int counter = 0, t, dx, dy, box_x, box_y;
			for(t = 0; t < *tile_count; ++t){
				for(dx = 0; dx < *N; ++dx) {
					
					box_x = x - dx;
					
					// if box_x is out of bounds, skip
					if(box_x < 0) continue;
					
					for(dy = 0; dy < *N; ++dy){
					
						box_y = y - dy;

						// if box_y is out of bounds, skip
						if (box_y < 0) continue;

						int wave_pos = t + (box_x + box_y * blockDim.x * gridDim.x) * *tile_count;
						
						// check if tile exists
						if(!wave[wave_pos]) continue;

						int tile_pos = dx + dy * (*N) + t * (*N) * (*N);	

						int color_id = tile_array[tile_pos];

						output[4 * chunk + 0] += colors_array[4 * color_id + 0];
						output[4 * chunk + 1] += colors_array[4 * color_id + 1];
						output[4 * chunk + 2] += colors_array[4 * color_id + 2];
						output[4 * chunk + 3] += colors_array[4 * color_id + 3];
						
						++counter;
					}
				}
			}
			
			if (counter == 0) counter = 1;
			
			output[4 * chunk + 0] /= counter;
			output[4 * chunk + 1] /= counter;
			output[4 * chunk + 2] /= counter;
			output[4 * chunk + 3] /= counter;
		}
	""")

	compute_colors = mod.get_function("compute_colors")

	st = time.time()
	compute_colors(wave_gpu, N_gpu, tile_count_gpu, tile_array_gpu, colors_array_gpu, output_gpu, block=(OUTPUT_X, OUTPUT_Y, 1), grid=(1, 1, 1))

	# Transfer the result back to the host
	drv.memcpy_dtoh(output, output_gpu)

	return [[tuple(i) for i in row] for row in output]