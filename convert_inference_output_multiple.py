# 11 Jul 2024
# edited from from./test_metrics/convert.py to account for confidence score
# for output of inference_with_score.py with folder as input rather than individual files

import argparse
import os

# Initialize parser
parser = argparse.ArgumentParser()

# Adding optional argument
parser.add_argument("-i", "--Input", required=True, help="Input file path")
parser.add_argument("-d", "--Directory", required=True, help="Output directory")

args = parser.parse_args()

if __name__ == '__main__':
    input_file = args.Input
    output_dir = args.Directory

    # Ensure the output directory exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(input_file, 'r') as file:
        next(file)  # Skip the header line
        lines = file.readlines()

    # Dictionary to store data for each image
    image_data = {}

    # Loop through each line in the file
    for line in lines:
        # Split the line into columns
        columns = line.split()

        # Extract the values for each column
        image = columns[0]
        label = columns[1]
        xmin = int(columns[2])
        xmax = int(columns[3])
        ymin = int(columns[4])
        ymax = int(columns[5])
        width = int(columns[6])
        height = int(columns[7])
        area = int(columns[8])
        score = float(columns[9])

        # Prepare the formatted line
        formatted_line = f"{label} {score:.7f} {xmin} {ymin} {xmax} {ymax}\n"

        # Append the formatted line to the appropriate image data list
        if image not in image_data:
            image_data[image] = []
        image_data[image].append(formatted_line)

    # Write the output files
    for image, data in image_data.items():
        output_file_path = os.path.join(output_dir, f"{image}.txt")
        with open(output_file_path, 'w') as outf:
            outf.writelines(data)
