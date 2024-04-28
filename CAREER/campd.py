import sys,os,glob
import io
import requests
import pandas as pd
import json
import pickle
import numpy as np
import datetime as dt
import logging
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Rectangle
from scipy.interpolate import interp1d, RegularGridInterpolator
import seaborn as sns
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from matplotlib.axes import Axes
from cartopy.mpl.geoaxes import GeoAxes
GeoAxes._pcolormesh_patched = Axes.pcolormesh
from netCDF4 import Dataset
from popy import Level3_Data, Level3_List, F_center2edge

class PointSource(object):
    '''class for a point emission source selected from CEMS facilities'''
    def __init__(self,lon,lat,name=None,emission_df=None,window_km=200,
                 start_km=5,end_km=100,nstep=20,start_dt=None,end_dt=None):
        '''
        lon/lat:
            center coordinate of the point source
        name:
            text description of the point source
        window_km:
            +/- winodw_km box from lon/lat will be the domain of satelite data,
            where fit_topo and fit_chem will be conducted
        nstep:
            number of distance steps to integrate flux to emission rate
        start/end_km:
            start/end of distances to integrate flux to emission rate
        emission_df:
            a dataframe for emission time series, i.e., indexed from CEMS.fedf
        start/end_dt:
            datetime objects
        '''
        self.logger = logging.getLogger(__name__)
        self.lon = lon
        self.lat = lat
        self.name = name
        self.emission_df = emission_df
        self.window_km = window_km
        self.nstep = nstep
        self.start_km = start_km
        self.end_km = end_km
        self.start_dt = start_dt
        self.end_dt = end_dt
        
        km_per_lat = 111
        km_per_lon = 111*np.cos(lat/180*np.pi)
        self.south = lat-window_km/km_per_lat
        self.north = lat+window_km/km_per_lat
        self.west = lon-window_km/km_per_lon
        self.east = lon+window_km/km_per_lon
        self.km_per_lat = km_per_lat
        self.km_per_lon = km_per_lon
    
    def regrid_tropomi(self,**kwargs):
        instrum = kwargs.pop('instrum','TROPOMI')
        product = kwargs.pop('product','NO2')
    def get_satellite_emissions(self,l3s,l3_all=None,fit_topo_kw=None,fit_chem_kw=None,
                          chem_min_column_amount=None,chem_max_wind_column=None):
        '''interface popy level 3 objects. 
        l3s:
            Level3_List object
        l3_all:
            aggregated l3 from l3s
        fit_topo_kw:
            keyword argument to fit_topography function
        fit_chem_kw:
            keyword argument to fit_chemistry function
        chem_min_column_amount:
            min column amount allowed in fit_chem
        chem_max_wind_column:
            max wind_column allowed in fit_chem
        '''
        l3_all = l3_all or l3s.aggregate()
        fit_topo_kw = fit_topo_kw or dict(max_iter=1)
        fit_chem_kw = fit_chem_kw or dict(resample_rule='month_of_year',
                                          max_iter=1,return_resampled=True)
        if 'resample_rule' not in fit_chem_kw.keys():
            fit_chem_kw['resample_rule'] = 'month_of_year'
        if 'return_resampled' not in fit_chem_kw.keys():
            fit_chem_kw['return_resampled'] = True
        if 'max_iter' not in fit_chem_kw.keys():
            fit_chem_kw['max_iter'] = 1
        ls = l3s.trim(west=self.west,east=self.east,south=self.south,north=self.north)
        l_all = l3_all.trim(west=self.west,east=self.east,south=self.south,north=self.north)
        
        chem_mask = np.ones(l_all['column_amount'].shape,dtype=bool)
        if chem_min_column_amount is not None:
            chem_mask = chem_mask & (l_all['column_amount']>=chem_min_column_amount)
        if chem_max_wind_column is not None:
            chem_mask = chem_mask & (l_all['wind_column']<=chem_max_wind_column)
        fit_chem_kw['mask'] = chem_mask
        ls.fit_topography(**fit_topo_kw)
        ls.fit_chemistry(**fit_chem_kw)
        l_all = ls.aggregate()
        lonmesh,latmesh = np.meshgrid(l_all['xgrid'],l_all['ygrid'])
        dist_to_f_km = np.sqrt(np.square((lonmesh-self.lon)*self.km_per_lon)+\
                               np.square((latmesh-self.lat)*self.km_per_lat)
                               )
        dist_steps_km = np.linspace(self.start_km,self.end_km,self.nstep)
        emission_rates = np.empty((len(ls),self.nstep),dtype=object)
        emission_rates_all = np.empty(self.nstep,dtype=object)
        for idist,dist_step in enumerate(dist_steps_km):
            mask = np.zeros(lonmesh.shape,dtype=bool)
            mask[dist_to_f_km <= dist_step] = True
            emission_rates_all[idist] = l_all.sum_by_mask(mask=mask)
            for il,ll in enumerate(ls):
                emission_rates[il,idist] = ll.sum_by_mask(mask=mask)
        l_all['dist_to_f_km'] = dist_to_f_km
        l_all['chem_mask'] = chem_mask
        self.l3_all = l_all
        # merge satellite emission rate and cems emission rate
        l3_df = ls.df
        fe0 = pd.DataFrame(self.emission_df['NOx (mol/s)'].resample(l3_df.index.freq).mean())
        fe0.index = fe0.index.to_period()
        fe0 = fe0.rename(columns={'NOx (mol/s)':'Facility NOx'})
        l3_df = l3_df.merge(fe0,left_index=True,right_index=True)
        if 'tropomi' in self.emission_df.keys():
            fe1 = pd.DataFrame(self.emission_df.loc[
                self.emission_df['tropomi']
                ]['NOx (mol/s)'].resample(l3_df.index.freq).mean())
            fe1.index = fe1.index.to_period()
            fe1 = fe1.rename(columns={'NOx (mol/s)':'Facility NOx with coverage'})
            l3_df = l3_df.merge(fe1,left_index=True,right_index=True)
        self.l3_df = l3_df
        self.dist_steps_km = dist_steps_km
        self.emission_rates = emission_rates
        self.emission_rates_all = emission_rates_all
    
    def slice_emission_rate(self,dist_slice=20,l3_flds=None):
        '''calculate emission rate over all time and for each satellite l3 time step
        within a given distance (dist_slice)'''
        l3_flds = l3_flds or ['wind_column','wind_column_topo','wind_column_topo_chem']
        self.emission_rate = {}
        for fld in l3_flds:
            y = [er[fld] for er in self.emission_rates_all]
            f = interp1d(self.dist_steps_km,y,bounds_error=False,fill_value='extrapolate')
            self.emission_rate[fld] = f(dist_slice)
            self.l3_df['er_{}'.format(fld)] = np.zeros(self.l3_df.shape[0])
            for il3,ers in enumerate(self.emission_rates):
                y = [er[fld] for er in ers]
                f = interp1d(self.dist_steps_km,y,bounds_error=False,fill_value='extrapolate')
                self.l3_df.loc[self.l3_df.index[il3],'er_{}'.format(fld)] = f(dist_slice)
        self.dist_slice = dist_slice
    
    def plot_4panel_diagnostic(self,figsize=(10,8),pc_kw=None,ts_kw=None,sns_kw=None):
        pc_kw = pc_kw or dict(vmin=0,vmax=10,cmap='rainbow')
        ts_kw = ts_kw or dict(ylim=[0,25])
        sns_kw = sns_kw or dict(annot=True,xticklabels=['DD','DDT','DDTC','F0','F1'],
                    yticklabels=['DD','DDT','DDTC','F0','F1'],fmt='.2f')
        label_position = pc_kw.pop('label_position',[0.5,0.15,0.3,0.035])
        map_position = pc_kw.pop('map_position',[0.05,0.8,0.2,0.18])
        l3_flds = ['wind_column','wind_column_topo','wind_column_topo_chem']
        l3_flds_alias = ['Directional derivative (DD)','DD+topography','DD+topography/chemstry']
        brightcc = ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE',
                    '#AA3377', '#BBBBBB', '#000000']
        ts_color = ts_kw.pop('color',brightcc[0:5])
        fig,axs = plt.subplots(2,2,figsize=figsize,constrained_layout=True)
        ax = axs[0,0]
        pc = ax.pcolormesh(*F_center2edge(self.l3_all['xgrid'],self.l3_all['ygrid']),
                           self.l3_all['wind_column_topo_chem']*1e9,**pc_kw)
        mapax = fig.add_axes(map_position,projection=ccrs.LambertConformal(),frameon=False)
        mapax.plot(self.lon,self.lat,'r*',transform=ccrs.PlateCarree())
        mapax.set_extent([-128,-65,22,50], ccrs.Geodetic())
        mapax.add_feature(cfeature.STATES,zorder=1,linewidth=.5,edgecolor='gray')
        cax = ax.inset_axes(label_position)
        cb = fig.colorbar(pc,ax=ax,cax=cax,orientation='horizontal',label=r'nmol m$^{-2}$ s$^{-1}$')
        ellipse = Ellipse((self.lon,self.lat), 
                          self.dist_slice*2/self.km_per_lon, 
                          self.dist_slice*2/self.km_per_lat, fill=False,ec='r',ls='--')
        ax.add_patch(ellipse)
        ellipse = Ellipse((self.lon,self.lat), 
                          self.end_km*2/self.km_per_lon, 
                          self.end_km*2/self.km_per_lat, fill=False,ec='r',ls='--')
        ax.add_patch(ellipse)

        ax = axs[0,1]
        for ifld,fld in enumerate(l3_flds):
            ax.plot(self.dist_steps_km,[er[fld] for er in self.emission_rates_all],
                    color=brightcc[ifld],label=l3_flds_alias[ifld],zorder=2)
        ax.axvline(self.dist_slice,ls='--',color='r',zorder=1,label='Integration distance')
        ax.legend()
        ax.grid(axis='both')
        ax.set_xlabel('Distance from facility [km]')
        ax.set_ylabel('Emission rate [mol/s]')

        ax = axs[1,0]
        self.l3_df.plot(ax=ax,y=['er_wind_column','er_wind_column_topo','er_wind_column_topo_chem',
                               'Facility NOx','Facility NOx with coverage'],
                      label=['Directional derivative (DD)','DD+topography (DDT)','DD+topography/chemstry (DDTC)',
                             'Facility NOx (F0)','Facility NOx with coverage (F1)'],color=ts_color,
                      **ts_kw)
        ax.legend()
        ax.set_ylabel('Emission rate [mol/s]')

        ax = axs[1,1]
        cor_mat = self.l3_df[['er_wind_column','er_wind_column_topo','er_wind_column_topo_chem',
                               'Facility NOx','Facility NOx with coverage']].corr()
        sns.heatmap(cor_mat,ax=ax,**sns_kw)
        fig.suptitle(self.name)
        return dict(fig=fig,cb=cb,axs=axs,mapax=mapax)

class CEMS():
    '''this class builts upon EPA's clean air markets program data portal: https://campd.epa.gov/
    the downloading part was inspired by its GitHup repository: https://github.com/USEPA/cam-api-examples
    it focuses on NOx emissions from energy generating units/facilities'''
    def __init__(self,API_key=None,start_dt=None,end_dt=None,
                 attributes_path_pattern=None,
                 emissions_path_pattern=None,
                 west=None,east=None,south=None,north=None):
        '''
        API_key:
            need one from https://www.epa.gov/airmarkets/cam-api-portal#/api-key-signup to download data
        start/end_dt:
            datetime objects accurate to the day
        attributes/emissions_path_pattern:
            path patterns for annual attributes and daily emission files. provided as default here. 
            can be updated later during data saving/loading
        west/east/south/north:
            lon/lat boundary. default to the CONUS
        '''
        self.logger = logging.getLogger(__name__)
        self.start_dt = start_dt or dt.datetime(2018,5,1)
        self.end_dt = end_dt or dt.datetime.now()
        self.west = west or -130.
        self.east = east or -63.
        self.south = south or 23.
        self.north = north or 51
        self.API_key = API_key
        self.attributes_path_pattern = attributes_path_pattern or \
        '/projects/academic/kangsun/data/CEMS/attributes/trimmed_%Y.csv'
        self.emissions_path_pattern = emissions_path_pattern or \
        '/projects/academic/kangsun/data/CEMS/emissions/%Y/%m/%d/%Y%m%d.csv'
    
    def return_PointSource(self,facilityId=None,rank=None,**kwargs):
        '''return a PointSource instance from facilities within CEMS instance'''
        if facilityId is None:
            facilityId = self.fadf.loc[self.fadf['rank']==rank].index[0]
        if not hasattr(self,'fedf'):
            self.logger.warning('loading the facility''s emission only')
            self.get_facilities_emission_rate(fadf=self.fadf.loc[[facilityId]])
        return PointSource(
            start_dt=self.start_dt,
            end_dt=self.end_dt,
            lon=self.fadf.loc[facilityId].longitude,
            lat=self.fadf.loc[facilityId].latitude,
            name=f'{self.fadf.loc[facilityId].facilityName}, {self.fadf.loc[facilityId].stateCode}',
            emission_df=self.fedf.loc[facilityId],**kwargs
        )
        
    def find_satellite_coverage(self,satellite_coverage_dict=None):
        '''add a column to fedf indicating if the time step has satellite coverage
        satellite_coverage_dict:
            a dict mapping satellite name to path of l3 files containing num_samples
        '''
        satellite_coverage_dict = satellite_coverage_dict or \
            {'tropomi':'/home/kangsun/data/S5PNO2/L3/num_samples/CONUS_%Y.pkl'}
        fedf = self.fedf
        fadf = self.fadf
        
        fids = fedf.index.levels[0]
        flatlon = np.array([fadf.loc[fid][['latitude','longitude']] for fid in fids],dtype=float)
        pdidx = pd.IndexSlice
        for k,v in satellite_coverage_dict.items():
            fedf = fedf.assign(**{k:np.zeros(fedf.shape[0],dtype=bool)})
            if k.lower() in {'tropomi','s5p','s5pno2'}:
                for yr in pd.period_range(self.start_dt,self.end_dt,freq='1Y'):
                    # load annual num_samples file for tropomi
                    fn = yr.strftime(v)
                    with open(fn,'rb') as f:
                        d = pickle.load(f)
                    # loop over dates
                    for date in pd.date_range(np.max([self.start_dt,yr.start_time]),
                                                np.min([self.end_dt,yr.end_time]),freq='1D'):
                        ns = d['num_samples'][d['dt_array']==date,...].squeeze()
                        f = RegularGridInterpolator((d['ygrid'],d['xgrid']), ns,method='nearest',
                                                    bounds_error=True,fill_value=0)
                        coverage = f((flatlon[:,0],flatlon[:,1])).astype(bool)
                        # loop over facilities
                        for fid,cov in zip(fids,coverage):
                            # tough one with multiindexing
                            fedf.loc[pdidx[fid,fedf.index.get_level_values(1).date==date.date()],
                                     pdidx[k]] = cov
            else:
                self.logger.warning('{} is not implemented yet, returning all false'.format(k))
                continue
        self.fedf = fedf
    
    def plot_facility_map(self,fadf=None,max_nfacility=None,ax=None,reset_extent=False,add_text=False,**kwargs):
        '''plot facilities as dots on a map. dot size corresponds to self.fadf[sdata_column], 
        where sdata_column defaults to 'noxMassLbs'
        '''
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        # workaround for cartopy 0.16
        from matplotlib.axes import Axes
        from cartopy.mpl.geoaxes import GeoAxes
        GeoAxes._pcolormesh_patched = Axes.pcolormesh
        if ax is None:
            fig,ax = plt.subplots(1,1,figsize=(10,5),subplot_kw={"projection": ccrs.PlateCarree()})
        else:
            fig = None
        cartopy_scale = kwargs.pop('cartopy_scale','110m')
        sc_leg_loc = kwargs.pop('sc_leg_loc','lower right')
        sc_leg_fmt = kwargs.pop('sc_leg_fmt','{x:.2f}')
        sc_leg_title = kwargs.pop('sc_leg_title',"Emitted NOx [lbs]")
        if fadf is None:
            fadf = self.fadf
        # assume fadf is sorted by noxMassLbs
        df = fadf.iloc[0:max_nfacility]
        sdata_column = kwargs.pop('sdata_column','noxMassLbs')
        sdata = df[sdata_column]
        sdata_func = kwargs.pop('sdata_func',None)
        if sdata_func is not None:
            sdata = sdata_func(sdata)
        sdata_min = kwargs.pop('sdata_min',np.nanmin(sdata))
        sdata_max = kwargs.pop('sdata_max',np.nanmax(sdata))
        sdata_min_size = kwargs.pop('sdata_min_size',25)
        sdata_max_size = kwargs.pop('sdata_max_size',100)
        # normalize to 0-1
        sdata = (sdata-sdata_min)/(sdata_max-sdata_min)
        # normalize to sdata_min_size-sdata_max_size
        sdata = sdata*(sdata_max_size-sdata_min_size)+sdata_min_size
        sc = ax.scatter(df['longitude'],df['latitude'],s=sdata,**kwargs)
        if sc_leg_loc is None:
            leg_sc = None
        else:
            handles, labels = sc.legend_elements(prop="sizes", alpha=0.6, num=7,fmt=sc_leg_fmt,
                                                 func=lambda x:(x-sdata_min_size)\
                                                 /(sdata_max_size-sdata_min_size)\
                                                *(sdata_max-sdata_min)+sdata_min)
            leg_sc = ax.legend(handles, labels, title=sc_leg_title,ncol=3,loc=sc_leg_loc)
            ax.add_artist(leg_sc)
        if reset_extent:
            ax.set_extent([self.west,self.east,self.south,self.north])
        if cartopy_scale is not None:
            ax.coastlines(resolution=cartopy_scale, color='black', linewidth=1)
            ax.add_feature(cfeature.STATES.with_scale(cartopy_scale), facecolor='None', edgecolor='k', 
                           linestyle='-',zorder=0,lw=0.5)
        if add_text:
            from adjustText import adjust_text
            texts = [ax.text(row.longitude,row.latitude,row.facilityName,fontsize=10)\
                     for irow,row in df.iterrows()]
            adjust_text(texts,
                        x=df['longitude'].to_numpy(),
                        y=df['longitude'].to_numpy(),ax=ax,
                        expand_text=(1.1, 1.2))
        return dict(fig=fig,ax=ax,sc=sc,leg_sc=leg_sc)
    
    def trim_unit_attributes(self,new_attributes_pattern,load_emissions_kw=None):
        '''load_emissions becomes too slow with large spatiotemporal windows
        run this once to get annual NOx for unit/facility to easily remove small ones.
        see ub ccr:/projects/academic/kangsun/data/CEMS/trim_attributes.py for example
        new_attributes_pattern:
            path pattern to save the trimmed/noxMass-added attributes table
        load_emissions_kw:
            keyword arguments to self.load_emissions
        '''
        load_emissions_kw = load_emissions_kw or {}
        load_emissions_kw['if_unit_emissions'] = True
        self.load_emissions(**load_emissions_kw)
        tuadf = []
        for year in pd.period_range(self.start_dt,self.end_dt,freq='1Y'):
            left_df = self.uadf[['noxMassLbs', 'index', 'stateCode', 'facilityName', 'facilityId',
       'unitId', 'latitude', 'longitude', 'year','primaryFuelInfo',
       'secondaryFuelInfo','maxHourlyHIRate']]
            left_df = left_df.loc[left_df['year']==year.year]
            right_df = self.fadf[['noxMassLbs','year']]
            right_df = right_df.loc[right_df['year']==year.year][['noxMassLbs']]
            df = pd.merge(left_df,right_df,left_on='facilityId',
                          right_index=True,suffixes=('','Facility'),
                          sort=False)#.sort_index()
            cols = list(df)
            cols.insert(0, cols.pop(cols.index('noxMassLbsFacility')))
            cols.insert(0, cols.pop(cols.index('index')))
            df = df.loc[:, cols]
            df.to_csv(year.strftime(new_attributes_pattern),
                      index=False,index_label='index')
            tuadf.append(df)
        return pd.concat(tuadf)
    
    def get_facilities_emission_rate(self,fadf=None,emissions_path_pattern=None,states=None,
                                     local_hours=None):
        '''
        a more recent version of load_emissions
        '''
        emissions_path_pattern = emissions_path_pattern or self.emissions_path_pattern
        if fadf is None:
            fadf = self.fadf
        uedf = []
        for date in pd.period_range(self.start_dt,self.end_dt,freq='1D'):
            filename = date.strftime(emissions_path_pattern)
            if not os.path.exists(filename):
                self.logger.warning('{} does not exist!'.format(filename))
                continue
            if date.day == 1:
                logging.info('loading emission file {}'.format(filename))
            edf = pd.read_csv(filename)
            edf = edf.loc[edf['Facility ID'].isin(fadf.index)]
            if local_hours is not None:
                edf = edf.loc[pd.to_datetime(edf['local_dt']).dt.hour.isin(local_hours)]
            edf['local_dt'] = pd.to_datetime(edf['local_dt'])
            uedf.append(edf)
        uedf = pd.concat(uedf).reset_index()
        fedf = uedf.groupby(['Facility ID','local_dt']
                           ).aggregate({'NOx Mass (lbs)':lambda x:np.sum(x)*0.453592/0.046/3600,#lbs to mol/s
                                        'SO2 Mass (lbs)':lambda x:np.sum(x)*0.453592/0.064/3600,#lbs to mol/s
                                        'CO2 Mass (short tons)':lambda x:np.sum(x)*907.185/0.044/3600,#short tons to mol/s
                                        'Facility Name':lambda x:x.iloc[0],
                                        'State':lambda x:x.iloc[0],
                                        'Gross Load (MW)':'sum',
                                        'Heat Input (mmBtu)':'sum'}
                                      ).rename(columns={'NOx Mass (lbs)':'NOx (mol/s)',
                                                       'SO2 Mass (lbs)':'SO2 (mol/s)',
                                                       'CO2 Mass (short tons)':'CO2 (mol/s)'})
        self.uedf = uedf
        self.fedf = fedf
        
    def subset_facilities(self,n_facility_with_most_NOx=10,enforce_presence_all_years=True):
        if not hasattr(self,'uadf'):
            self.logger.info('run load_emissions to get uadf first')
            self.load_emissions(if_unit_emissions=False,n_facility_with_most_NOx=None)
        fadf = self.uadf.groupby('facilityId'
                                ).aggregate({'noxMassLbs':'sum',
                                            'year':lambda x:len(x.unique()),
                                            'primaryFuelInfo':lambda x:'/'.join(x.dropna().unique()),
                                            'facilityName':lambda x:x.iloc[0],
                                            'stateCode':lambda x:x.iloc[0],
                                            'latitude':'mean',
                                            'longitude':'mean'}
                                           ).sort_values('noxMassLbs',ascending=False)
        if enforce_presence_all_years:
            mask = fadf['year']==len(pd.period_range(self.start_dt,self.end_dt,freq='1Y'))
            if np.sum(mask) != fadf.shape[0]:
                self.logger.warning('only {} facilities have attributes in all years out of {}'.format(
                    np.sum(mask),fadf.shape[0]))
            fadf = fadf.loc[mask]
        fadf['rank'] = np.arange(fadf.shape[0],dtype=int)+1
        self.fadf = fadf.iloc[0:n_facility_with_most_NOx]
    
    def load_emissions(self,attributes_path_pattern=None,emissions_path_pattern=None,states=None,
                      local_hours=None,if_unit_emissions=True,if_facility_emissions=False,
                      n_facility_with_most_NOx=None):
        '''
        attributes/emissions_path_pattern:
            path patterns for annual attributes and daily emission files. 
            good practice is to save trimmed attributes using self.trim_unit_attributes (takes hours), 
            then only load a small number of largest facilities, where one can turn off emissions file loading
        states:
            if provided, should be a list of state codes, e.g., ['TX']
        local_hours:
            if_provided, should be a list of int hours, e.g., [13]
        if_unit_emissions:
            if load emissions files. slow if space*time*number of units is large. can be off if only looking at unit/facility
            attributes when trimmed files are already saved
        if_facility_emissions:
            if groupby facility and calculate facility level emissions. may need a separate function
        n_facility_with_most_NOx:
            number of largest facilites (not units) to include
        if emissions are all on, adds the following to the object:
            uadf = unit attributes data frame; fadf = facility attributes data frame
            uedf = unit emissions data frame; fadf = facility emissions data frame
        '''
        attributes_path_pattern = attributes_path_pattern or self.attributes_path_pattern
        emissions_path_pattern = emissions_path_pattern or self.emissions_path_pattern
        func_1st = lambda x:x.iloc[0]
        
        uadf = []
        if if_unit_emissions:
            uedf = []
        else:
            if_facility_emissions=False# facility level emissions impossible without unit level emissions
        
        for year in pd.period_range(self.start_dt,self.end_dt,freq='1Y'):
            csv_name = year.strftime(attributes_path_pattern)
            self.logger.info('loading attribute file {}'.format(csv_name))
            adf = pd.read_csv(csv_name)
            mask = (adf['longitude']>=self.west)&(adf['longitude']<=self.east)&\
            (adf['latitude']>=self.south)&(adf['latitude']<=self.north)
            if states is not None:
                mask = mask & (adf['stateCode'].isin(states))
            adf = adf.loc[mask]
            # keep only units in the n_facility_with_most_NOx largest facilities
            if 'noxMassLbsFacility' in adf.keys():
                nfac = n_facility_with_most_NOx
            else:
                nfac = None
            if nfac is not None:
                gadf = adf.groupby('facilityId'
                                  ).aggregate({'noxMassLbsFacility':func_1st}
                                             ).sort_values('noxMassLbsFacility',ascending=False
                                                          ).iloc[0:nfac,:]
                adf = adf.loc[adf['facilityId'].isin(gadf.index)]
            uadf.append(adf)
            if not if_unit_emissions:
                continue# load emission files otherwise
            for date in pd.period_range(np.max([self.start_dt,year.start_time]),
                                       np.min([self.end_dt,year.end_time]),freq='1D'):
                filename = date.strftime(emissions_path_pattern)
                if not os.path.exists(filename):
                    self.logger.warning('{} does not exist!'.format(filename))
                    continue
                if date.day == 1:
                    self.logger.info('loading emission file {}'.format(filename))
                edf = pd.read_csv(filename)
                edf = edf.loc[edf['Facility ID'].isin(adf['facilityId'])]
                if local_hours is not None:
                    edf = edf.loc[pd.to_datetime(edf['local_dt']).dt.hour.isin(local_hours)]
                uedf.append(edf)
        
        self.uadf = pd.concat(uadf).reset_index()
        if if_unit_emissions:
            self.uedf = pd.concat(uedf).reset_index()
        if 'noxMassLbs' not in self.uadf.keys():
            if not if_unit_emissions:
                self.logging.warning('Please turn on if_unit_emissions')
                return
            # add column for nox emission in attribute df
            noxMassLbs = np.zeros(self.uadf.shape[0])
            for i,(irow,row) in enumerate(self.uadf.iterrows()):
                noxMassLbs[i] = self.uedf.loc[(self.uedf['Facility ID']==row.facilityId)&\
                                     (self.uedf['Unit ID']==row.unitId)]['NOx Mass (lbs)'].sum()
            self.uadf.insert(loc=0,column='noxMassLbs',value=noxMassLbs)
            self.uadf = self.uadf.sort_values('noxMassLbs',ascending=False).reset_index(drop=True)
        
        self.fadf = self.uadf.groupby('facilityId').aggregate({
            'noxMassLbs':'sum',
            'year':'mean',
            'facilityName':func_1st,
            'stateCode':func_1st,
            'latitude':'mean',
            'longitude':'mean'}).sort_values('noxMassLbs',ascending=False)
        if if_facility_emissions:
            self.fedf = self.uedf.groupby(['Facility ID','local_dt']).aggregate({
                'NOx Mass (lbs)':'sum',
                'SO2 Mass (lbs)':'sum',
                'CO2 Mass (short tons)':'sum',
                'Facility Name':func_1st,
                'State':func_1st,
                'Operating Time':'sum',
                'Gross Load (MW)':'sum',
                'Heat Input (mmBtu)':'sum'})
    
    def download_attributes(self,attributes_path_pattern=None,API_key=None):
        self.attributes_path_pattern = attributes_path_pattern
        API_key = API_key or self.API_key
        if API_key is None:
            self.logger.error('you need API key, see https://www.epa.gov/airmarkets/cam-api-portal#/api-key-signup')
            return
        # making get request using the facilities/attributes endpoint
        streamingUrl = "https://api.epa.gov/easey/streaming-services/facilities/attributes"
        for year in pd.period_range(self.start_dt,self.end_dt,freq='1Y'):
            parameters = {
                'api_key': API_key,
                'year': year.year
            }
            self.logger.info('fetching year {}'.format(year.year))
            streamingResponse = requests.get(streamingUrl, params=parameters,timeout=5)
            self.logger.info("Status code: "+str(streamingResponse.status_code))
            # collecting data as a data frame
            df = pd.DataFrame(streamingResponse.json())
            csv_name = year.strftime(attributes_path_pattern)
            self.logger.info('saving to {}'.format(csv_name))
            df.to_csv(csv_name,index=False)
    
    def resave_emissions(self,emissions_path_pattern=None,API_key=None,cols_to_keep=None):
        emissions_path_pattern = self.emissions_path_pattern or emissions_path_pattern
        API_key = API_key or self.API_key
        if API_key is None:
            self.logger.error('you need API key, see https://www.epa.gov/airmarkets/cam-api-portal#/api-key-signup')
            return
        cols_to_keep = cols_to_keep or ['State','Facility Name','Facility ID',
                                        'Unit ID','Operating Time','Gross Load (MW)','Heat Input (mmBtu)',
                                        'SO2 Mass (lbs)','CO2 Mass (short tons)','NOx Mass (lbs)']
        # S3 bucket url base + s3Path (in get request) = the full path to the files
        BUCKET_URL_BASE = 'https://api.epa.gov/easey/bulk-files/'
        parameters = {
            'api_key': API_key
        }
        self.logger.info('getting bulk file lists...')
        # executing get request
        response = requests.get("https://api.epa.gov/easey/camd-services/bulk-files", params=parameters)
        # printing status code
        self.logger.info("Status code: "+str(response.status_code))
        # converting the content from json format to a data frame
        resjson = response.content.decode('utf8').replace("'", '"')
        data = json.loads(resjson)
        s = json.dumps(data, indent=4)
        jsonread = pd.read_json(s)
        pddf = pd.DataFrame(jsonread)
        bulkFiles = pd.concat([pddf.drop(['metadata'], axis=1), pddf['metadata'].apply(pd.Series)], axis=1)
        for year in pd.period_range(self.start_dt,self.end_dt,freq='1Y'):
            # year-quarter bulkFiles, yqbf
            yqbf = bulkFiles.loc[(bulkFiles['dataType']=='Emissions') &\
                                 (bulkFiles['filename'].str.contains('emissions-hourly-{}-q'.format(year.year)))]
            # loop over quarter, save daily
            for irow, row in yqbf.iterrows():
                url = BUCKET_URL_BASE+row.s3Path
                self.logger.info('retrieving data from {}'.format(url))
                res = requests.get(url).content
                # dataframe for the quarter
                df = pd.read_csv(io.StringIO(res.decode('utf-8')))
                col_date = pd.to_datetime(df['Date'])
                col_dt = col_date+pd.to_timedelta(df['Hour'],unit='h')
                # loop over dates
                for date in col_date.unique():
                    mask = col_date == date
                    daily_df = pd.concat([pd.Series(data=col_dt[mask],name='local_dt'),df.loc[mask][cols_to_keep]],axis=1)
                    filename = pd.to_datetime(date).strftime(emissions_path_pattern)
                    os.makedirs(os.path.split(filename)[0],exist_ok=True)
                    daily_df.to_csv(filename,index=False)

class Geos_Cf():
    '''paying a tribute to geos.py at
    https://github.com/Kang-Sun-CfA/Methane/blob/master/l2_met/geos.py'''
    def __init__(self,start_dt,end_dt,utc_hours=None,
                 west=-130.,east=-63.,south=23.,north=52.,\
                 time_collection='tavg_1hr',dir_pattern=None):
        self.logger = logging.getLogger(__name__)
        self.west = west
        self.east = east
        self.south = south
        self.north = north
        if time_collection=='tavg_1hr':
            times = pd.date_range(start_dt.replace(microsecond=0,second=0,minute=30),
                                  end_dt,freq='1h')
        if utc_hours is not None:
            times = times[times.hour.isin(utc_hours)]
        self.times = times
        self.dir_pattern = dir_pattern
    
    def download_and_resave(self,url_patterns=None,fields=None,dir_pattern=None,delete_nc4=False):
        dir_pattern = dir_pattern or self.dir_pattern
        if url_patterns is None:
            url_patterns = ['https://portal.nccs.nasa.gov/datashare/gmao/geos-cf/v1/das/Y%Y/M%m/D%d/GEOS-CF.v01.rpl.chm_tavg_1hr_g1440x721_v36.%Y%m%d_%H%Mz.nc4']
        if fields is None:
            fields = [['NO2','NO']]
        for time in self.times:
            save_dir = time.strftime(dir_pattern)
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            os.chdir(save_dir)
            for pattern,flds in zip(url_patterns,fields):
                url = time.strftime(pattern)
                fn = os.path.join(save_dir,url.split('/')[-1])
                try:
                    if not os.path.exists(fn):
                        os.system(f'wget -np -q {url}')
                        self.logger.info(f'{fn} downloaded')
                        # r = requests.get(url)
                        # with open(fn,'wb') as f:
                        #     f.write(r.content)
                        #     self.logger.info(f'{fn} downloaded')
                    else:
                        self.logger.warning(f'{fn} already exists, skip downloading...')
                    d = {}
                    with Dataset(fn,'r') as nc:
                        lon = nc['lon'][:].filled(np.nan)
                        lat = nc['lat'][:].filled(np.nan)
                        xmask = (lon>=self.west)&(lon<self.east)
                        ymask = (lat>=self.south)&(lat<self.north)
                        lon = lon[xmask]
                        lat = lat[ymask]
                        for fld in flds:
                            d[fld] = \
                            nc[fld][0,:,ymask,xmask].filled(np.nan)
                        d['lon'] = lon
                        d['lat'] = lat
                    pkl_fn = os.path.splitext(fn)[0]+'.pkl'
                    with open(pkl_fn,'wb') as f:
                        pickle.dump(d,f,pickle.HIGHEST_PROTOCOL)
                        self.logger.info(f'{pkl_fn} resaved')
                    if delete_nc4:
                        os.remove(fn)
                except Exception as e:
                    self.logger.warning(f'{fn} gives error:')
                    self.logger.warning(e)
    
    def load(self,collection_dict=None,dir_pattern=None):
        if collection_dict is None:
            collection_dict = {'chm_tavg_1hr_g1440x721_v36':['NO2','NO','OH_mmr']}
        data = {k:{} for k in collection_dict.keys()}
        dir_pattern = dir_pattern or self.dir_pattern
        for itime,time in enumerate(self.times):
            save_dir = time.strftime(dir_pattern)
            for collection,fields in collection_dict.items():
                pkl_fn = os.path.join(save_dir,f'{collection}.pkl')
                with open(pkl_fn,'rb') as f:
                    d = pickle.load(f)
                if not hasattr(self,'xgrid'):
                    self.xgrid = d['lon']
                    self.ygrid = d['lat']
                for field in fields:
                    if field not in data[collection].keys():
                        data[collection][field] = \
                        np.zeros((len(self.times),*d[field].shape))
                    data[collection][field][itime,...] = d[field]
        self.data = data