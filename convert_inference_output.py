# 11 Jul 2024
# edited from from./test_metrics/convert.py to account for confidence score

import argparse

# Initialize parser
parser = argparse.ArgumentParser()

# Adding optional argument
parser.add_argument("-o", "--Output", help = "Show Output")
parser.add_argument("-i", "--Input", help = "Show Output")

inFile = ""
outFile = ""
# Read arguments from command line
args = parser.parse_args()

if __name__ == '__main__':
    if args.Input and args.Output:
        print("start converting")
        inFile = args.Input

        outFile = args.Output

        file = open(inFile, 'r')
        next(file)
        outf = open(outFile,'w')
        lines = file.readlines()

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

            outf.write(f"{label} {score} {xmin} {ymin} {xmax} {ymax}\n")
        outf.close()
