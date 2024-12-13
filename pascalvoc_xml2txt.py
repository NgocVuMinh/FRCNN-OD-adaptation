import os
import xml.etree.ElementTree as ET
import argparse

# Initialize parser
parser = argparse.ArgumentParser()

# Adding optional argument
parser.add_argument("-i", "--input", required=True, help="input dir")
parser.add_argument("-o", "--output", required=True, help="output directory")

args = parser.parse_args()

def convert_xml_to_txt(input_file, output_file):
    try:
        tree = ET.parse(input_file)
    except:
        print(input_file)
        pass
    root = tree.getroot()

    with open(output_file, 'w') as f:
        for obj in root.findall('object'):
            name = obj.find('name').text
            bndbox = obj.find('bndbox')
            xmin = bndbox.find('xmin').text
            ymin = bndbox.find('ymin').text
            xmax = bndbox.find('xmax').text
            ymax = bndbox.find('ymax').text
            f.write(f"{name} {xmin} {ymin} {xmax} {ymax}\n")

def process_folder(input_folder, output_folder):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    for filename in os.listdir(input_folder):
        if filename.endswith('.xml'):
            input_file = os.path.join(input_folder, filename)
            output_file = os.path.join(output_folder, filename.replace('.xml', '.txt'))
            convert_xml_to_txt(input_file, output_file)

#input_file = './convert-yolo-to-pascalvoc/outputs_val/BA_2862_jpg.rf.145006bf7f2d9a6622d9b2a48137aa4a.xml'
#output_file = 'BA_2862_jpg_test_xml2txt.txt'
input_folder = args.input
output_folder = args.output
process_folder(input_folder, output_folder)
