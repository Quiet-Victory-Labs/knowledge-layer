import Alert from '@mui/material/Alert';
import MaterialButton from '@mui/material/Button';
import MaterialCheckbox from '@mui/material/Checkbox';
import CircularProgress from '@mui/material/CircularProgress';
import MaterialDialog from '@mui/material/Dialog';
import DialogContentText from '@mui/material/DialogContentText';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import MaterialIconButton from '@mui/material/IconButton';
import ListSubheader from '@mui/material/ListSubheader';
import Menu from '@mui/material/Menu';
import MenuItem from '@mui/material/MenuItem';
import Tab from '@mui/material/Tab';
import MaterialTabs from '@mui/material/Tabs';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import {
  Children,
  cloneElement,
  createContext,
  createElement,
  forwardRef,
  isValidElement,
  useContext,
  useId,
  useState,
  type CSSProperties,
  type ComponentProps,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type MouseEvent,
  type PropsWithChildren,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';

type Tone = 'gray' | 'green' | 'red' | 'orange' | 'amber';
type Size = '1' | '2' | '3' | '4' | '5' | '6' | '7';

const theme = createTheme({
  palette: {
    primary: { main: '#18794e' },
    error: { main: '#ce2c31' },
    warning: { main: '#ad5700' },
  },
  typography: { fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' },
  shape: { borderRadius: 8 },
  components: { MuiButton: { defaultProps: { disableElevation: true } } },
});

export function Theme({ children }: PropsWithChildren<{ accentColor?: string; grayColor?: string; radius?: string; appearance?: string }>) {
  return <ThemeProvider theme={theme}><div className="ui-theme">{children}</div></ThemeProvider>;
}

type ButtonProps = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'color'> & {
  variant?: 'solid' | 'soft' | 'ghost';
  color?: Tone;
  size?: Size;
  loading?: boolean;
  href?: string;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = 'solid', color = 'green', size = '2', loading = false, disabled, className, children, href, ...props
}, ref) {
  const mappedColor = color === 'red' ? 'error' : color === 'orange' || color === 'amber' ? 'warning' : color === 'gray' ? 'inherit' : 'primary';
  const mappedVariant = variant === 'solid' ? 'contained' : variant === 'soft' ? 'outlined' : 'text';
  return <MaterialButton
    {...props}
    ref={ref}
    href={href}
    variant={mappedVariant}
    color={mappedColor}
    size={size === '1' ? 'small' : size === '2' ? 'medium' : 'large'}
    disabled={disabled || loading}
    className={[className, 'ui-button', `ui-button-${variant}`].filter(Boolean).join(' ')}
    startIcon={loading ? <CircularProgress color="inherit" size={14} aria-hidden="true" /> : undefined}
  >{children}</MaterialButton>;
});

type IconButtonProps = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'color'> & { color?: Tone; variant?: 'ghost' };
export function IconButton({ color = 'gray', variant: _variant, ...props }: IconButtonProps) {
  return <MaterialIconButton {...props} color={color === 'gray' ? 'default' : color === 'red' ? 'error' : 'primary'} size="small" />;
}

type TextProps = HTMLAttributes<HTMLElement> & { as?: 'span' | 'p' | 'label' | 'div'; htmlFor?: string; size?: Size; weight?: 'regular' | 'medium' | 'bold'; color?: Tone };
export function Text({ as = 'span', size = '2', weight, color, className, children, ...props }: TextProps) {
  return createElement(as, { ...props, className: [className, 'ui-text', `ui-text-${size}`, weight && `ui-weight-${weight}`, color && `ui-color-${color}`].filter(Boolean).join(' ') }, children);
}

type HeadingProps = HTMLAttributes<HTMLHeadingElement> & { as?: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'; size?: Size };
export function Heading({ as = 'h2', size = '5', className, children, ...props }: HeadingProps) {
  return createElement(as, { ...props, className: [className, 'ui-heading', `ui-heading-${size}`].filter(Boolean).join(' ') }, children);
}

const spacing: Record<string, string> = { '1': '4px', '2': '8px', '3': '12px', '4': '16px', '5': '24px' };
type FlexProps = HTMLAttributes<HTMLDivElement> & {
  direction?: 'row' | 'column'; gap?: string; align?: 'start' | 'center' | 'end'; justify?: 'start' | 'center' | 'end' | 'between';
  wrap?: 'wrap' | 'nowrap'; flexGrow?: string; flexBasis?: string; minWidth?: string; mt?: string;
};
export function Flex({ direction, gap, align, justify, wrap, flexGrow, flexBasis, minWidth, mt, style, ...props }: FlexProps) {
  const mapped: CSSProperties = {
    display: 'flex', flexDirection: direction ?? 'row', gap: gap ? spacing[gap] ?? gap : undefined,
    alignItems: align === 'start' ? 'flex-start' : align === 'end' ? 'flex-end' : align,
    justifyContent: justify === 'between' ? 'space-between' : justify === 'end' ? 'flex-end' : justify === 'start' ? 'flex-start' : justify,
    flexWrap: wrap, flexGrow: flexGrow ? Number(flexGrow) : undefined, flexBasis,
    minWidth, marginTop: mt ? spacing[mt] ?? mt : undefined, ...style,
  };
  return <div {...props} style={mapped} />;
}

export const TextField = {
  Root: forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function TextFieldRoot({ className, ...props }, ref) {
    return <input {...props} ref={ref} className={[className, 'ui-input'].filter(Boolean).join(' ')} />;
  }),
};

export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function TextArea({ className, ...props }, ref) {
  return <textarea {...props} ref={ref} className={[className, 'ui-input', 'ui-textarea'].filter(Boolean).join(' ')} />;
});

type SelectTriggerProps = SelectHTMLAttributes<HTMLSelectElement> & { variant?: 'ghost' };
function SelectTrigger(_props: SelectTriggerProps) { return null; }
function SelectContent({ children }: PropsWithChildren) { return <>{children}</>; }
function SelectItem({ children, value }: PropsWithChildren<{ value: string }>) { return <option value={value}>{children}</option>; }
function SelectRoot({ children, value, onValueChange, disabled }: PropsWithChildren<{ value: string; onValueChange: (value: string) => void; disabled?: boolean }>) {
  const entries = Children.toArray(children);
  const trigger = entries.find((entry): entry is ReactElement<SelectTriggerProps> => isValidElement(entry) && entry.type === SelectTrigger);
  const content = entries.find((entry): entry is ReactElement<PropsWithChildren> => isValidElement(entry) && entry.type === SelectContent);
  const { variant: _variant, className, ...triggerProps } = trigger?.props ?? {};
  return <select {...triggerProps} disabled={disabled} value={value} onChange={(event) => onValueChange(event.target.value)}
    onKeyDown={(event) => {
      triggerProps.onKeyDown?.(event);
      // A native select may submit its surrounding form on Enter after type-ahead.
      // Keep source selection separate from the explicit Connect source action.
      if (event.key === 'Enter' && event.currentTarget.form && !event.defaultPrevented) event.preventDefault();
    }}
    className={[className, 'ui-input', 'ui-select'].filter(Boolean).join(' ')}>{content?.props.children}</select>;
}
export const Select = { Root: SelectRoot, Trigger: SelectTrigger, Content: SelectContent, Item: SelectItem };

type CheckboxProps = Omit<ComponentProps<typeof MaterialCheckbox>, 'checked' | 'onChange'> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
};
export function Checkbox({ checked, onCheckedChange, ...props }: CheckboxProps) {
  return <MaterialCheckbox {...props} size="small" checked={checked} onChange={(_event, next) => onCheckedChange(next)} />;
}

type DialogState = { open: boolean; onOpenChange: (open: boolean) => void; titleId: string; descriptionId: string };
const DialogContext = createContext<DialogState | null>(null);
function useDialog() {
  const value = useContext(DialogContext);
  if (!value) throw new Error('Dialog components require Dialog.Root');
  return value;
}
function DialogRoot({ children, open, onOpenChange }: PropsWithChildren<{ open: boolean; onOpenChange: (open: boolean) => void }>) {
  const id = useId();
  return <DialogContext.Provider value={{ open, onOpenChange, titleId: `${id}-title`, descriptionId: `${id}-description` }}>{children}</DialogContext.Provider>;
}
function DialogTrigger({ children }: { children: ReactElement<{ onClick?: (event: MouseEvent) => void }> }) {
  const dialog = useDialog();
  return cloneElement(Children.only(children), { onClick: (event: MouseEvent) => { children.props.onClick?.(event); if (!event.defaultPrevented) dialog.onOpenChange(true); } });
}
function DialogClose({ children }: { children: ReactElement<{ onClick?: (event: MouseEvent) => void }> }) {
  const dialog = useDialog();
  return cloneElement(Children.only(children), { onClick: (event: MouseEvent) => { children.props.onClick?.(event); if (!event.defaultPrevented) dialog.onOpenChange(false); } });
}
type DismissEvent = { preventDefault: () => void };
function DialogContent({ children, className, maxWidth = '600px', onEscapeKeyDown, onInteractOutside }: PropsWithChildren<{
  className?: string; maxWidth?: string; onEscapeKeyDown?: (event: DismissEvent) => void; onInteractOutside?: (event: DismissEvent) => void;
}>) {
  const dialog = useDialog();
  return <MaterialDialog open={dialog.open} onClose={(_event, reason) => {
    let blocked = false;
    const dismissEvent = { preventDefault: () => { blocked = true; } };
    if (reason === 'escapeKeyDown') onEscapeKeyDown?.(dismissEvent);
    if (reason === 'backdropClick') onInteractOutside?.(dismissEvent);
    if (!blocked) dialog.onOpenChange(false);
  }} aria-labelledby={dialog.titleId} aria-describedby={dialog.descriptionId} scroll="paper" fullWidth
    slotProps={{ paper: { sx: { width: `min(100%, ${maxWidth})`, maxWidth, maxHeight: 'min(90vh, 960px)' } } }}>
    <div className={['ui-dialog-content', className].filter(Boolean).join(' ')}>{children}</div>
  </MaterialDialog>;
}
function DialogTitleView({ children }: PropsWithChildren<{ size?: Size }>) {
  const dialog = useDialog();
  return <DialogTitle id={dialog.titleId} sx={{ padding: 0, marginBottom: 0.5, fontWeight: 650 }}>{children}</DialogTitle>;
}
function DialogDescription({ children }: PropsWithChildren<{ size?: Size; color?: Tone }>) {
  const dialog = useDialog();
  return <DialogContentText id={dialog.descriptionId} sx={{ marginBottom: 2 }}>{children}</DialogContentText>;
}
export const Dialog = { Root: DialogRoot, Trigger: DialogTrigger, Content: DialogContent, Title: DialogTitleView, Description: DialogDescription, Close: DialogClose };

type MenuState = { anchor: HTMLElement | null; open: (anchor: HTMLElement) => void; close: () => void };
const MenuContext = createContext<MenuState | null>(null);
function useMenu() {
  const value = useContext(MenuContext);
  if (!value) throw new Error('DropdownMenu components require DropdownMenu.Root');
  return value;
}
function MenuRoot({ children }: PropsWithChildren) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  return <MenuContext.Provider value={{ anchor, open: setAnchor, close: () => setAnchor(null) }}>{children}</MenuContext.Provider>;
}
function MenuTrigger({ children }: { children: ReactElement<{ onClick?: (event: MouseEvent) => void }> }) {
  const menu = useMenu();
  return cloneElement(Children.only(children), { onClick: (event: MouseEvent) => { children.props.onClick?.(event); if (!event.defaultPrevented) menu.open(event.currentTarget as HTMLElement); } });
}
function MenuContent({ children }: PropsWithChildren) {
  const menu = useMenu();
  return <Menu anchorEl={menu.anchor} open={Boolean(menu.anchor)} onClose={menu.close} slotProps={{ paper: { sx: { minWidth: 190, maxWidth: 'min(90vw, 320px)' } } }}>{children}</Menu>;
}
function MenuItemView({ children, disabled, onSelect }: PropsWithChildren<{ disabled?: boolean; onSelect?: () => void }>) {
  const menu = useMenu();
  return <MenuItem disabled={disabled} onClick={() => { onSelect?.(); menu.close(); }}>{children}</MenuItem>;
}
function MenuLabel({ children }: PropsWithChildren) { return <ListSubheader disableSticky>{children}</ListSubheader>; }
export const DropdownMenu = { Root: MenuRoot, Trigger: MenuTrigger, Content: MenuContent, Item: MenuItemView, Separator: Divider, Label: MenuLabel };

type TabsState = { value: string; onValueChange: (value: string) => void; id: string };
const TabsContext = createContext<TabsState | null>(null);
function useTabs() {
  const value = useContext(TabsContext);
  if (!value) throw new Error('Tabs components require Tabs.Root');
  return value;
}
function TabsRoot({ children, value, onValueChange }: PropsWithChildren<{ value: string; onValueChange: (value: string) => void }>) {
  const id = useId();
  return <TabsContext.Provider value={{ value, onValueChange, id }}>{children}</TabsContext.Provider>;
}
function TabsList({ children }: PropsWithChildren) {
  const tabs = useTabs();
  const triggers = Children.toArray(children).filter((child): child is ReactElement<PropsWithChildren<{ value: string }>> =>
    isValidElement<PropsWithChildren<{ value: string }>>(child) && child.type === TabsTrigger);
  return <MaterialTabs value={tabs.value} onChange={(_event, next: string) => tabs.onValueChange(next)} variant="scrollable" scrollButtons="auto">
    {triggers.map((trigger) => <Tab key={trigger.props.value} id={`${tabs.id}-${trigger.props.value}-tab`}
      aria-controls={`${tabs.id}-${trigger.props.value}-panel`} label={trigger.props.children} value={trigger.props.value} />)}
  </MaterialTabs>;
}
function TabsTrigger(_props: PropsWithChildren<{ value: string }>) { return null; }
function TabsContent({ children, value, className }: PropsWithChildren<{ value: string; className?: string }>) {
  const tabs = useTabs();
  return tabs.value === value ? <div role="tabpanel" id={`${tabs.id}-${value}-panel`} aria-labelledby={`${tabs.id}-${value}-tab`} className={className}>{children}</div> : null;
}
export const Tabs = { Root: TabsRoot, List: TabsList, Trigger: TabsTrigger, Content: TabsContent };

function CalloutRoot({ children, color = 'gray', role, mt }: PropsWithChildren<{ color?: Tone; role?: string; mt?: string }>) {
  const severity = color === 'red' ? 'error' : color === 'green' ? 'success' : color === 'amber' || color === 'orange' ? 'warning' : 'info';
  return <Alert role={role} severity={severity} sx={{ marginTop: mt ? spacing[mt] ?? mt : undefined, alignItems: 'center' }}>{children}</Alert>;
}
function CalloutText({ children }: PropsWithChildren) { return <span>{children}</span>; }
export const Callout = { Root: CalloutRoot, Text: CalloutText };
export function Separator(_props: { size?: Size }) { return <Divider />; }
export function Spinner(_props: { size?: Size }) { return <CircularProgress size={16} aria-label="Loading" />; }
