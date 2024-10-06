import numpy as np
import torch
import tqdm
from torch.nn import CTCLoss
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter

from dataset import new_dataloader, get_longest_mel_id
from duration_extraction import extract_durations_with_dijkstra
from model import Aligner
from paths import Paths
from text import Tokenizer
from utils import to_device

class Trainer:

    def __init__(self, paths: Paths, device: torch.device) -> None:
        self.paths = paths
        self.device = device  # Store the device
        self.writer = SummaryWriter(log_dir=paths.checkpoint_dir / 'tensorboard')
        self.ctc_loss = CTCLoss()

        # Used for generating plots
        longest_id = get_longest_mel_id(dataset_path=self.paths.data_dir / 'dataset.pkl')
        self.longest_mel = np.load(str(paths.mel_dir / f'{longest_id}.npy'), allow_pickle=False)
        self.longest_tokens = np.load(str(paths.token_dir / f'{longest_id}.npy'), allow_pickle=False)

    def train(self, checkpoint: dict, train_params: dict) -> None:
        lr = train_params['learning_rate']
        epochs = train_params['epochs']
        batch_size = train_params['batch_size']
        ckpt_steps = train_params['checkpoint_steps']
        plot_steps = train_params['plot_steps']
        
        device = self.device  # Use the device from the constructor

        config = checkpoint['config']
        symbols = checkpoint['symbols']
        
        # NOTE: For phonemes, pass for_phonemes = true
        tokenizer = Tokenizer(symbols, for_phonemes=True)

        model = Aligner.from_checkpoint(checkpoint).to(device)
        optim = Adam(model.parameters())
        optim.load_state_dict(checkpoint['optim'])

        for g in optim.param_groups:
            g['lr'] = lr

        dataloader = new_dataloader(dataset_path=self.paths.data_dir / 'dataset.pkl', mel_dir=self.paths.mel_dir,
                                    token_dir=self.paths.token_dir, batch_size=batch_size)

        start_epoch = model.get_step() // len(dataloader)

        for epoch in range(start_epoch + 1, epochs + 1):
            pbar = tqdm.tqdm(enumerate(dataloader, 1), total=len(dataloader))
            for i, batch in pbar:
                tokens, mel, tokens_len, mel_len = to_device(batch, device)

                pred = model(mel)
                pred = pred.transpose(0, 1).log_softmax(2)

                loss = self.ctc_loss(pred, tokens, mel_len, tokens_len)
                pbar.set_description(desc=f'Epoch: {epoch} | Step {model.get_step()} '
                                          f'| Loss: {loss:#.4}', refresh=True)

                if not torch.isnan(loss) and not torch.isinf(loss):
                    optim.zero_grad()
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optim.step()

                self.writer.add_scalar('CTC_Loss', loss.item(), global_step=model.get_step())
                self.writer.add_scalar('Params/batch_size', batch_size, global_step=model.get_step())
                self.writer.add_scalar('Params/learning_rate', lr, global_step=model.get_step())

                if model.get_step() % ckpt_steps == 0:
                    torch.save({'model': model.state_dict(), 'optim': optim.state_dict(),
                                'config': config, 'symbols': symbols},
                               self.paths.checkpoint_dir / f'model_step_{model.get_step() // 1000}k.pt')

                if model.get_step() % plot_steps == 0:
                    self.generate_plots(model, tokenizer)

            latest_checkpoint = self.paths.checkpoint_dir / 'latest_model.pt'
            torch.save({'model': model.state_dict(), 'optim': optim.state_dict(),
                        'config': config, 'symbols': symbols},
                       latest_checkpoint)

    def generate_plots(self, model: Aligner, tokenizer: Tokenizer) -> None:
        model.eval()
        device = next(model.parameters()).device
        longest_mel = torch.tensor(self.longest_mel).unsqueeze(0).float().to(device)
        pred = model(longest_mel)[0].detach().cpu().softmax(dim=-1)
        durations = extract_durations_with_dijkstra(self.longest_tokens, pred.numpy())
        pred_max = pred.max(1)[1].numpy().tolist()
        pred_text = tokenizer.decode(pred_max)
        target_text = tokenizer.decode(self.longest_tokens)
        # print('target_text---',target_text)

        # commented for character TTE
        # # NOTE: Input phoneme sequence is space separated for readability, so we need to strip that out when matching it with the durations.  
        # target_duration_rep = ''.join(c * durations[i] for i, c in enumerate(target_text.split(" ")))
        
        # NEIL: done for character TTE
        lst = target_text.split("  ")
        tmp = []
        for i in range(len(lst)):
            tmp.extend(lst[i].split(" "))
        target_duration_rep = ''.join(c * durations[i] for i, c in enumerate(tmp))
        
        self.writer.add_text('Text/Prediction', '    ' + pred_text, global_step=model.get_step())
        self.writer.add_text('Text/Target_Duration_Repeated',
                             '    ' + target_duration_rep, global_step=model.get_step())
        self.writer.add_text('Text/Target', '    ' + target_text, global_step=model.get_step())
        model.train()
