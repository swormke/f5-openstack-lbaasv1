#!/bin/sh

#  docScript2.sh
#  
#
#  Created by Jodie Putrino on 10/27/15.
#

FILES=*.md
for f in $FILES
do
# extension="${f##*.}"
filename="${f%.*}"
echo "Converting $f to $filename.html"
`pandoc $f -t html5 -o $filename.html`
# uncomment this line to delete the source file.
# rm $f
done