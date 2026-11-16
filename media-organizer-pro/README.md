# Media Organizer Pro v13.0

## 启动
- 双击 `Media Organizer Pro.app`
- 若 macOS 拦截，右键应用选择“打开”
- 备用入口：双击 `一键启动.command`

## 一键流程
点击源素材旁的“多选”，可在同一个 macOS 选择窗口中一次选择多个文件夹；再选择至少一个备份目标后，点击“一键扫描并开始备份”。软件会自动完成扫描、元数据读取、目录规划、复制、校验和报告生成，不再要求先扫描再开始。

## 重复与同名文件
- 内容完全一致：通过文件大小和所选校验值确认，跳过重复副本，并在任务记录中标记 `DUPLICATE_SKIPPED`。
- 文件名相同但内容不同：两个文件都保留。新文件使用 `原名__拍摄时间__校验值前8位.ext` 命名，例如 `IMG_0001__20260806_124510__a1b2c3d4.JPG`。
- 如果新名称仍冲突，会追加 `__2`、`__3`，绝不覆盖已有文件。
- Verified Move 模式只有在所有目标均确认存在完全一致副本或新副本校验成功后，才删除源文件。

建议安装 ExifTool 获得最完整的相机与拍摄时间识别：

```bash
brew install exiftool
```

v13.0 会在常见 Homebrew 路径中查找 ExifTool，并规避 macOS 不支持 `C.UTF-8` 时导致的 ExifTool 启动失败。


## 多源文件夹
- macOS 中点击“多选”后，可按住 Command 选择多个文件夹，再点击“选择”。
- 所有已选文件夹会作为同一批任务扫描并统一备份。
- 如果同时选择了父文件夹和其中的子文件夹，软件只保留父文件夹，避免同一素材被扫描两次。
- 来自不同源文件夹的完全重复素材仍按校验值去重；同名但内容不同的素材仍按拍摄时间与短校验值安全改名。


## v12.4
- Start/Stop controls are fixed at the bottom of the sidebar and remain visible in windowed mode.
- Duplicate handling now supports Skip, Keep an auto-renamed copy, or copy to a Duplicates review folder.


## 12.8 多来源管理
- 可分多次点击“添加来源”，新来源会追加到列表，不覆盖已选来源。
- 来源列表支持多选后“删除所选”。
- 可使用“清空来源”重置当前任务来源。
- 父目录与其子目录同时添加时，仅保留父目录，避免重复扫描。

## v13.0 — 全目标重复校验

重复检测不再局限于本次计算出的目标路径。选择“跳过完全重复文件”时，软件会先建立每个备份目标的全目录文件大小索引；遇到同尺寸候选后，再计算完整 SHA-256/MD5。即使更改日期结构、相机分类或目录组织方式，只要目标中的任意位置已经存在内容完全一致的文件，就不会再次复制。重复报告会记录实际找到的已有文件路径。

任务运行期间，每个新完成的校验副本也会立即加入索引。因此，同一批多来源中内容相同但文件名不同的素材也只会复制一次。

## Download
A prebuilt macOS app (`Media Organizer Pro.app`) is available on the Releases page. Or run from source: `python3 media_organizer_pro.py`.
