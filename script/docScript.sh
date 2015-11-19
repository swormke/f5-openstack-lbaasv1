#!/usr/bin/env sh

#  docScript.sh
#  
#
#  Created by Jodie Putrino on 10/27/15.
#

#install gems in Gemfile
#bundle install

# remove the project directory if it currently exists
#rm -rf ./f5-os-lbaasv1


# copy content of doc directory into project folder
echo "copying doc directory into f5-os-lbaasv1"
cp -R ./$TRAVISREPOSLUG/doc f5-openstack-docs/f5-os-lbaasv1

# build site
echo "building site with jekyll"
bundle exec jekyll build --config _lbaasconfig.yml -s f5-openstack-docs -d ./site_build

#echo "proofing site with htmlproofer"
#bundle exec htmlproof ./site_build

echo "copying docs to $HOME"
cp -R ./site_build/f5-os-lbaasv1 $HOME/f5-os-lbaasv1

echo "listing contents of $HOME/f5-os-lbaasv1"
ls -a $HOME/f5-os-lbaasv1

