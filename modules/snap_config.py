def snapSettingConfig():
    '''Disable QGIS native snapping; all snapping is handled by snap_utils.'''
    from . import snap_manager
    snap_manager._apply_to_qgis()
